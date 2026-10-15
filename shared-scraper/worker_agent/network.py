"""Key-free routing diagnostics and guarded portal HTTPS connections.

No tunnel lifecycle changes or production requests occur at import time.
"""
import ipaddress
import ctypes
from functools import lru_cache
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from control_plane.contracts import utcnow


class PortalRouteError(RuntimeError):
    """Portal traffic must stop until the configured tunnel is repaired."""


def config_routes(path):
    """Extract only public routing fields; parser errors never include a line."""
    try:
        lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
        sections, allowed, dns = [], [], []
        section = None
        for line in lines:
            line = line.split('#', 1)[0].strip()
            if line.startswith('['):
                section = line.lower()
                sections.append(section)
            elif '=' in line:
                key, value = (part.strip() for part in line.split('=', 1))
                if key.lower() == 'allowedips' and section == '[peer]':
                    allowed.extend(ipaddress.ip_network(v.strip(), strict=False) for v in value.split(','))
                elif key.lower() == 'dns' and section == '[interface]':
                    dns.extend(ipaddress.ip_address(v.strip()) for v in value.split(','))
        if sections != ['[interface]', '[peer]'] or not allowed:
            raise ValueError()
        return tuple(allowed), tuple(dns)
    except Exception:
        raise PortalRouteError('Invalid or unreadable worker VPN routing configuration.') from None


def covered(address, networks):
    ip = ipaddress.ip_address(address)
    return any(ip.version == net.version and ip in net for net in networks)


def resolve(host):
    try:
        addresses = sorted({str(ipaddress.ip_address(row[4][0])) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        if not addresses or len(addresses) > 32:
            raise ValueError()
        return addresses
    except Exception:
        raise RuntimeError('Hostname resolution failed.') from None


class _SockaddrIn(ctypes.Structure):
    _fields_ = [('family', ctypes.c_uint16), ('port', ctypes.c_uint16),
                ('address', ctypes.c_ubyte * 4), ('padding', ctypes.c_ubyte * 8)]


class _SockaddrIn6(ctypes.Structure):
    _fields_ = [('family', ctypes.c_uint16), ('port', ctypes.c_uint16),
                ('flowinfo', ctypes.c_uint32), ('address', ctypes.c_ubyte * 16),
                ('scope', ctypes.c_uint32)]


@lru_cache(maxsize=1)
def _ip_helper():
    # Load lazily: fixture workers on other platforms never require this DLL.
    api = ctypes.WinDLL('iphlpapi.dll', winmode=0x00000800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
    api.GetBestInterfaceEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    api.GetBestInterfaceEx.restype = ctypes.c_uint32
    api.ConvertInterfaceIndexToLuid.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64)]
    api.ConvertInterfaceIndexToLuid.restype = ctypes.c_uint32
    api.ConvertInterfaceLuidToAlias.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_wchar_p, ctypes.c_size_t]
    api.ConvertInterfaceLuidToAlias.restype = ctypes.c_uint32
    return api


def windows_routes(addresses):
    """Query the IPv4/IPv6 best-route interface in-process using Windows IP Helper.

    No subprocess, network connection, elevation or tunnel changes are needed.
    Any native error fails closed; there is no guessed/default interface fallback.
    """
    try:
        if not addresses:
            raise ValueError()
        api, result = _ip_helper(), {}
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if getattr(ip, 'scope_id', None) is not None:
                raise ValueError()  # Reviewed portal destinations must be unscoped.
            sockaddr = _SockaddrIn() if ip.version == 4 else _SockaddrIn6()
            sockaddr.family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
            sockaddr.address[:] = ip.packed
            index, luid, alias = ctypes.c_uint32(), ctypes.c_uint64(), ctypes.create_unicode_buffer(257)
            if (api.GetBestInterfaceEx(ctypes.byref(sockaddr), ctypes.byref(index)) or not index.value
                    or api.ConvertInterfaceIndexToLuid(index.value, ctypes.byref(luid))
                    or api.ConvertInterfaceLuidToAlias(ctypes.byref(luid), alias, len(alias)) or not alias.value):
                raise ValueError()
            result[str(ip)] = alias.value
        return result
    except Exception:
        raise RuntimeError('Unable to verify Windows destination routes.') from None


class FixtureNetwork:
    """Explicit simulation, never evidence of live routing."""
    def __init__(self, settings):
        self.mode = settings.network_mode
        self.state = {'requested_mode': self.mode, 'effective_mode': 'offline', 'simulated': True,
                      'portal': 'not_checked', 'coordinator': 'not_checked', 'reason': None}

    def preflight(self):
        self.state.update(portal='simulated', checked_at=utcnow().isoformat())

    def invalidate(self):
        pass

    def portal_check(self, url=None):
        self.state['portal'] = 'simulated'

    def coordinator_check(self):
        self.state.update(coordinator='simulated', checked_at=utcnow().isoformat())

    def snapshot(self):
        return dict(self.state)

    def report(self, **values):
        if 'portal' in values or 'coordinator' in values:
            values['checked_at'] = utcnow().isoformat()
        self.state.update(values)


class WorkerNetwork:
    ROUTE_CACHE_SECONDS = 2.0

    def __init__(self, settings, resolver=resolve, routes=windows_routes, config_path=None, clock=time.monotonic):
        self.settings = settings
        self.resolver, self.routes = resolver, routes
        directory = settings.vpn_config_dir or Path(__file__).resolve().parents[1] / 'vpn-configs'
        self.path = config_path or directory / (settings.vpn_config + '.conf')
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.networks = ()
        self.dns = ()
        self.signature = None
        self.clock = clock
        self.route_cache = {}
        self.state = {'requested_mode': settings.network_mode, 'effective_mode': 'offline', 'simulated': False,
                      'portal': 'not_checked', 'coordinator': 'not_checked', 'reason': None}

    def snapshot(self):
        # UI status must not wait for a DNS or native route query.
        with self.state_lock:
            return dict(self.state)

    def report(self, **values):
        with self.state_lock:
            if 'portal' in values or 'coordinator' in values:
                values['checked_at'] = utcnow().isoformat()
            self.state.update(values)

    def _signature(self):
        stat = self.path.stat()
        return stat.st_mtime_ns, stat.st_size

    def invalidate(self):
        """Discard route proofs whenever the owned tunnel lifecycle changes."""
        with self.lock:
            self.route_cache.clear()

    def _check_config(self):
        if self.signature is None or self._signature() != self.signature:
            raise PortalRouteError('VPN configuration changed; stop and restart the bounded worker cycle.')

    def _routes(self, role, addresses):
        # DNS resolution and config checks remain fresh at every guard layer.
        # Only successful native route lookups are reused, for at most 2 seconds.
        key = (self.signature, self.settings.vpn_config, tuple(sorted(set(addresses))))
        now = self.clock()
        cached = self.route_cache.get(role)
        if cached and cached[0] != key:
            self.invalidate()  # A destination change invalidates every proof.
            cached = None
        if cached and now < cached[1]:
            return cached[2]
        routes = self.routes(addresses)
        if not addresses or any(not routes.get(ip) for ip in addresses):
            raise RuntimeError('Unable to verify destination routes.')
        self.route_cache[role] = (key, now + self.ROUTE_CACHE_SECONDS, dict(routes))
        return routes

    def preflight(self):
        with self.lock:
            self.invalidate()
            self.signature = None
            try:
                before = self._signature()
                self.networks, self.dns = config_routes(self.path)
                self.signature = self._signature()
                if before != self.signature:
                    raise PortalRouteError('VPN configuration changed during preflight; retry with a stable configuration.')
                if self.settings.network_mode == 'split':
                    if any(net.prefixlen == 0 for net in self.networks):
                        raise PortalRouteError('Split mode cannot use a default VPN route.')
                    if any(not covered(ip, self.networks) for ip in self.dns):
                        raise PortalRouteError('VPN DNS is outside AllowedIPs; prepare a reviewed DNS configuration before split mode.')
                for host in self.settings.portal_hosts:
                    self._addresses(host)
                self.report(portal='configuration_checked', reason=None)
            except Exception as exc:
                self.signature = None
                self.invalidate()
                self.report(portal='blocked', reason=str(exc))
                raise PortalRouteError(str(exc)) from None

    def _addresses(self, host):
        addresses = self.resolver(host)
        if not addresses or any(not covered(a, self.networks) for a in addresses):
            raise PortalRouteError('A resolved portal address is outside AllowedIPs; update the reviewed worker configuration.')
        return addresses

    def portal_check(self, url=None):
        with self.lock:
            try:
                self._check_config()
                hosts = self.settings.portal_hosts
                if url:
                    parsed = urlparse(url)
                    if parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.port not in (None, 443) or parsed.username or parsed.password:
                        raise PortalRouteError('Portal request or redirect uses an unreviewed destination.')
                    hosts = (parsed.hostname,)
                selected = {}
                for host in hosts:
                    addresses = self._addresses(host)
                    routes = self._routes('portal:' + host, addresses)
                    if any(routes.get(ip, '').lower() != self.settings.vpn_config.lower() for ip in addresses):
                        raise PortalRouteError('Portal destination is not routed through the owned WireGuard tunnel.')
                    selected[host] = addresses
                if self.dns:
                    dns_addresses = [str(ip) for ip in self.dns]
                    routes = self._routes('dns', dns_addresses)
                    if any(routes.get(ip, '').lower() != self.settings.vpn_config.lower() for ip in dns_addresses):
                        raise PortalRouteError('Configured VPN DNS is not routed through the owned tunnel.')
                self.report(portal='verified')
                return selected
            except Exception as exc:
                self.invalidate()
                self.report(portal='blocked', reason=str(exc))
                raise PortalRouteError(str(exc)) from None

    def coordinator_check(self):
        with self.lock:
            try:
                self._check_config()
                host = urlparse(self.settings.coordinator_url).hostname
                addresses = self.resolver(host)
                routes = self._routes('coordinator', addresses)
                if self.signature is None or not addresses or any(not routes.get(ip) or covered(ip, self.networks) or routes[ip].lower() == self.settings.vpn_config.lower() for ip in addresses):
                    raise RuntimeError('Coordinator is not using a normal route outside the worker tunnel.')
                self.report(coordinator='route_verified')
            except Exception:
                self.invalidate()
                self.report(coordinator='unreachable', effective_mode='offline',
                            reason='Coordinator DNS or normal route verification failed.')
                raise RuntimeError('Coordinator DNS or normal route verification failed.') from None

    def install(self, session):
        # Guard redirects and keep-alive requests too. Environment proxies would
        # circumvent destination routing and are deliberately disabled here.
        session.trust_env = False
        session.proxies.clear()
        session.mount('https://', portal_adapter(self))
        session.mount('http://', portal_adapter(self))


def portal_adapter(guard):
    """Pin new sockets to vetted IPs while preserving hostname TLS/SNI checks."""
    from requests.adapters import HTTPAdapter
    from urllib3 import PoolManager
    from urllib3.connection import HTTPSConnection
    from urllib3.connectionpool import HTTPSConnectionPool
    from urllib3.util.connection import create_connection

    class RoutedConnection(HTTPSConnection):
        def request(self, *args, **kwargs):
            addresses = guard.portal_check('https://' + self.host)[self.host]
            if self.sock and str(ipaddress.ip_address(self.sock.getpeername()[0])) not in addresses:
                # A keep-alive socket must not keep using an old DNS destination.
                self.close()
            return super().request(*args, **kwargs)

        def _new_conn(self):
            addresses = guard.portal_check('https://' + self.host)[self.host]
            last = None
            for address in addresses:
                try:
                    return create_connection((address, self.port), self.timeout,
                                             self.source_address, self.socket_options)
                except OSError as exc:
                    last = exc
            raise OSError('Unable to connect to a verified portal destination.') from last

    class RoutedPool(HTTPSConnectionPool):
        ConnectionCls = RoutedConnection

    class Adapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {'https': RoutedPool}

        def send(self, request, **kwargs):
            guard.portal_check(request.url)
            if kwargs.get('proxies') or kwargs.get('verify') is False:
                raise PortalRouteError('Portal proxies and disabled TLS validation are not supported by guarded routing.')
            return super().send(request, **kwargs)

    return Adapter()
