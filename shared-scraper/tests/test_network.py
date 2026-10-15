from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from worker_agent.config import WorkerSettings
from worker_agent.network import WorkerNetwork, PortalRouteError, config_routes, portal_adapter, windows_routes


class NetworkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'fixture-vpn.conf'
        self.settings = WorkerSettings('https://coordinator.test', 'worker-token-at-least-24-characters',
                                      'local-token-at-least-24-characters', self.root, mode='live',
                                      vpn_config='fixture-vpn', network_mode='split', portal_hosts=('portal.test',))
        self.addresses = {'portal.test': ['192.0.2.15'], 'coordinator.test': ['203.0.113.9']}
        self.interfaces = {'192.0.2.15': 'fixture-vpn', '203.0.113.9': 'Ethernet'}
        self.write_config()
        self.guard = WorkerNetwork(self.settings, resolver=lambda h: self.addresses[h],
                                   routes=lambda ips: {ip: self.interfaces[ip] for ip in ips}, config_path=self.path)

    def write_config(self, allowed='192.0.2.15/32', dns=''):
        self.path.write_text('[Interface]\nPrivateKey = SYNTHETIC-SECRET-DO-NOT-PRINT\n' +
                             ('DNS = ' + dns + '\n' if dns else '') +
                             '[Peer]\nAllowedIPs = ' + allowed + '\n', encoding='utf-8')

    def test_preflight_and_active_routes_are_separate(self):
        self.guard.preflight()
        self.assertEqual(self.guard.snapshot()['portal'], 'configuration_checked')
        self.assertEqual(self.guard.snapshot()['effective_mode'], 'offline')
        self.guard.portal_check()
        self.guard.coordinator_check()
        self.assertEqual(self.guard.snapshot()['portal'], 'verified')
        self.assertEqual(self.guard.snapshot()['coordinator'], 'route_verified')
        self.assertNotIn('SYNTHETIC', str(self.guard.snapshot()))

    def test_uncovered_ipv4_and_ipv6_dns_fail_before_connect(self):
        for dns in ('10.2.0.1', '2a07:b944::2:1'):
            with self.subTest(dns=dns):
                self.write_config(dns=dns)
                with self.assertRaisesRegex(PortalRouteError, 'DNS is outside'):
                    self.guard.preflight()

    def test_covered_dns_and_both_portal_address_families(self):
        self.write_config('192.0.2.15/32,2001:db8::15/128,10.2.0.1/32', '10.2.0.1')
        self.addresses['portal.test'].append('2001:db8::15')
        self.interfaces['2001:db8::15'] = 'fixture-vpn'
        self.interfaces['10.2.0.1'] = 'fixture-vpn'
        self.guard.preflight()
        self.guard.portal_check()

    def test_uncovered_ipv6_or_changed_portal_ip_blocks_requests(self):
        self.guard.preflight()
        for ip in ('2001:db8::15', '192.0.2.16'):
            with self.subTest(ip=ip):
                self.addresses['portal.test'] = [ip]
                with self.assertRaisesRegex(PortalRouteError, 'outside AllowedIPs'):
                    self.guard.portal_check()

    def test_wrong_route_and_changed_config_fail_closed(self):
        self.guard.preflight()
        self.interfaces['192.0.2.15'] = 'Ethernet'
        with self.assertRaisesRegex(PortalRouteError, 'owned WireGuard'):
            self.guard.portal_check()
        self.interfaces['192.0.2.15'] = 'fixture-vpn'
        self.path.write_text(self.path.read_text() + '# changed configuration\n')
        with self.assertRaisesRegex(PortalRouteError, 'configuration changed'):
            self.guard.portal_check()

    def test_coordinator_dns_failure_or_tunnel_route_downgrades(self):
        self.guard.preflight()
        self.guard.report(effective_mode='split')
        self.interfaces['203.0.113.9'] = 'fixture-vpn'
        with self.assertRaises(RuntimeError):
            self.guard.coordinator_check()
        self.assertEqual(self.guard.snapshot()['effective_mode'], 'offline')
        self.guard.resolver = lambda h: (_ for _ in ()).throw(OSError('DNS failed'))
        with self.assertRaises(RuntimeError):
            self.guard.coordinator_check()

    def test_full_tunnel_remains_supported_only_in_offline_mode(self):
        self.write_config('0.0.0.0/0', '10.2.0.1')
        with self.assertRaises(PortalRouteError):
            self.guard.preflight()
        self.guard.settings = replace(self.settings, network_mode='offline')
        self.interfaces['10.2.0.1'] = 'fixture-vpn'
        self.guard.preflight()
        self.guard.portal_check()

    def test_bad_config_never_exposes_secret_lines(self):
        self.write_config('SYNTHETIC-SECRET-DO-NOT-PRINT')
        with self.assertRaises(PortalRouteError) as raised:
            config_routes(self.path)
        self.assertNotIn('SYNTHETIC', str(raised.exception))

    def test_route_cache_reuses_checks_but_expires_and_fails_closed(self):
        clock = [100.0]
        self.guard.clock = lambda: clock[0]
        self.guard.routes = unittest.mock.Mock(side_effect=lambda ips: {ip: self.interfaces[ip] for ip in ips})
        self.guard.preflight()
        for _ in range(10):
            self.guard.portal_check()
            self.guard.coordinator_check()
        self.assertEqual(self.guard.routes.call_count, 2)
        self.interfaces['192.0.2.15'] = 'Ethernet'
        clock[0] += self.guard.ROUTE_CACHE_SECONDS
        with self.assertRaisesRegex(PortalRouteError, 'owned WireGuard'):
            self.guard.portal_check()
        self.assertEqual(self.guard.route_cache, {})
        self.interfaces['192.0.2.15'] = 'fixture-vpn'
        self.guard.portal_check()
        self.assertEqual(self.guard.routes.call_count, 4)

    def test_dns_destination_change_invalidates_all_route_proofs(self):
        self.write_config('192.0.2.15/32,192.0.2.16/32')
        self.interfaces['192.0.2.16'] = 'fixture-vpn'
        self.guard.routes = unittest.mock.Mock(side_effect=lambda ips: {ip: self.interfaces[ip] for ip in ips})
        self.guard.preflight()
        self.guard.portal_check()
        self.guard.coordinator_check()
        self.addresses['portal.test'] = ['192.0.2.16']
        self.guard.portal_check()
        self.guard.coordinator_check()
        self.assertEqual(self.guard.routes.call_count, 4)
        self.addresses['coordinator.test'] = ['192.0.2.15']
        with self.assertRaises(RuntimeError):
            self.guard.coordinator_check()
        self.assertEqual(self.guard.route_cache, {})

    def test_preflight_lifecycle_and_config_changes_discard_cached_routes(self):
        self.guard.routes = unittest.mock.Mock(side_effect=lambda ips: {ip: self.interfaces[ip] for ip in ips})
        self.guard.preflight()
        self.guard.portal_check()
        self.guard.invalidate()
        self.guard.portal_check()
        self.guard.preflight()
        self.guard.portal_check()
        self.assertEqual(self.guard.routes.call_count, 3)
        self.path.write_text(self.path.read_text() + '# changed\n')
        with self.assertRaises(RuntimeError):
            self.guard.coordinator_check()
        self.assertEqual(self.guard.route_cache, {})
        self.write_config('not-an-ip')
        with self.assertRaises(PortalRouteError):
            self.guard.preflight()
        self.assertIsNone(self.guard.signature)

    def test_native_route_errors_fail_closed_without_shell_fallback(self):
        from unittest.mock import Mock
        api = Mock()
        api.GetBestInterfaceEx.return_value = 50
        with patch('worker_agent.network._ip_helper', return_value=api), patch('subprocess.run') as shell:
            with self.assertRaisesRegex(RuntimeError, 'Unable to verify Windows'):
                windows_routes(['192.0.2.15'])
            api.ConvertInterfaceIndexToLuid.assert_not_called()
            shell.assert_not_called()
        for addresses in ([], ['bad-address'], ['fe80::1%12']):
            with self.subTest(addresses=addresses), self.assertRaises(RuntimeError):
                windows_routes(addresses)

    @unittest.skipUnless(__import__('sys').platform == 'win32', 'Windows IP Helper integration')
    def test_native_windows_ipv4_ipv6_loopback_routes_without_subprocess(self):
        with patch('subprocess.run', side_effect=AssertionError('Route lookup must stay in process')):
            routes = windows_routes(['127.0.0.1', '::1'])
        self.assertTrue(routes['127.0.0.1'])
        self.assertEqual(routes['127.0.0.1'], routes['::1'])

    def test_request_adapter_blocks_redirect_proxy_and_insecure_tls(self):
        import requests
        self.guard.preflight()
        adapter = portal_adapter(self.guard)
        for url, options in [('https://unreviewed.test/file', {}), ('http://portal.test/', {}),
                             ('https://portal.test/', {'verify': False}),
                             ('https://portal.test/', {'proxies': {'https': 'http://proxy.test'}})]:
            with self.subTest(url=url, options=options):
                with self.assertRaises(PortalRouteError):
                    adapter.send(requests.Request('GET', url).prepare(), **options)
        with patch('requests.adapters.HTTPAdapter.send', return_value='sent'):
            self.assertEqual(adapter.send(requests.Request('GET', 'https://portal.test/').prepare()), 'sent')
        adapter.close()

    def test_connection_pins_vetted_ip_and_preserves_tls_hostname(self):
        self.guard.preflight()
        with patch('urllib3.util.connection.create_connection', return_value='socket') as connect:
            adapter = portal_adapter(self.guard)
            pool = adapter.poolmanager.connection_from_host('portal.test', scheme='https')
            connection = pool._new_conn()
            self.assertEqual(connection._new_conn(), 'socket')
            self.assertEqual(connect.call_args.args[0], ('192.0.2.15', 443))
        self.assertEqual(connection.host, 'portal.test')
        adapter.close()

    def test_settings_reject_unsafe_tunnel_and_network_modes(self):
        for values in ({'vpn_config': '../outside'}, {'network_mode': 'auto'},
                       {'portal_hosts': ('https://portal.test/',)}, {'network_check_seconds': 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(self.settings, **values)

    def test_health_requires_expected_response_and_never_follows_redirects(self):
        from worker_agent.transport import CoordinatorClient
        import requests
        client = CoordinatorClient(self.settings)
        for status, content in ((302, b'{}'), (200, b'{"status":"wrong"}')):
            response = requests.Response()
            response.status_code, response._content = status, content
            with patch.object(client.session, 'get', return_value=response) as call:
                with self.assertRaises(RuntimeError):
                    client.health()
                self.assertFalse(call.call_args.kwargs['allow_redirects'])

    def test_audit_does_not_start_tunnel_or_probe_https_without_connected(self):
        import contextlib
        import io
        from worker_agent.network_check import main
        with patch('worker_agent.network_check.WorkerNetwork') as network, patch('subprocess.run') as run:
            network.return_value.snapshot.return_value = {'portal': 'configuration_checked'}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--vpn-config', 'fixture-vpn', '--coordinator-url', 'https://coordinator.test']), 0)
            network.return_value.preflight.assert_called_once()
            network.return_value.portal_check.assert_not_called()
            network.return_value.install.assert_not_called()
            run.assert_not_called()

    def test_private_config_directory_is_used_by_guard_and_audit(self):
        import contextlib
        import io
        from worker_agent.network_check import main
        guard = WorkerNetwork(replace(self.settings, vpn_config_dir=self.root),
                              resolver=lambda h: self.addresses[h],
                              routes=lambda ips: {ip: self.interfaces[ip] for ip in ips})
        guard.preflight()
        self.assertEqual(guard.path, self.path)
        with patch('worker_agent.network_check.WorkerNetwork') as factory:
            factory.return_value.snapshot.return_value = {'portal': 'configuration_checked'}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--vpn-config', 'fixture-vpn', '--config-dir', str(self.root),
                                       '--coordinator-url', 'https://coordinator.test']), 0)
            self.assertEqual(factory.call_args.args[0].vpn_config_dir, self.root.resolve())

    def test_wireguard_connect_uses_private_config_directory(self):
        from worker_agent.engine import WireGuardVPN
        with patch('vpn_rotator.connect', return_value=True) as connect, patch.object(WireGuardVPN, '_state', return_value=4):
            vpn = WireGuardVPN('fixture-vpn', self.root)
            vpn.connect()
            connect.assert_called_once_with('fixture-vpn', config_dir=self.root)

    def test_tls_connection_validates_original_hostname_with_pinned_ip(self):
        # A real loopback TLS server verifies the custom pool integration, not
        # only its configuration. The certificate never leaves this test.
        import datetime as dt
        import ssl
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'portal.test')])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('portal.test')]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        cert_path, key_path = self.root / 'test-cert.pem', self.root / 'test-key.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        class Handler(BaseHTTPRequestHandler):
            def handle(self):
                try:
                    super().handle()
                except OSError:
                    pass  # The negative TLS check intentionally resets the socket.

            def do_GET(self):
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'ok')
                except OSError:
                    pass  # Expected when the hostname rejection closes TLS.
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.write_config('127.0.0.1/32')
        self.addresses['portal.test'] = ['127.0.0.1']
        self.interfaces['127.0.0.1'] = 'fixture-vpn'
        self.guard.preflight()
        adapter = portal_adapter(self.guard)
        try:
            pool = adapter.poolmanager.connection_from_host('portal.test', port=server.server_port, scheme='https',
                                                           pool_kwargs={'cert_reqs': ssl.CERT_REQUIRED, 'ca_certs': str(cert_path)})
            self.assertEqual(pool.urlopen('GET', '/', retries=False, timeout=3).data, b'ok')
            # Requiring a different hostname must fail certificate validation.
            wrong = adapter.poolmanager.connection_from_host('portal.test', port=server.server_port, scheme='https',
                                                            pool_kwargs={'cert_reqs': ssl.CERT_REQUIRED, 'ca_certs': str(cert_path),
                                                                         'assert_hostname': 'wrong.test'})
            from urllib3.exceptions import SSLError
            with self.assertRaises(SSLError):
                wrong.urlopen('GET', '/', retries=False, timeout=3)
        finally:
            adapter.close()
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
