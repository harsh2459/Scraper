import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class WorkerSettings:
    coordinator_url: str
    worker_token: str
    local_token: str
    storage_dir: Path
    port: int = 5011
    batch_size: int = 5
    batch_seconds: int = 1200
    mode: str = 'fixture'
    vpn_config: str = ''
    account_file: Path | None = None
    max_pending: int = 100
    min_free_bytes: int = 256 * 1024 * 1024
    full_check_days: int = 30
    network_mode: str = 'offline'
    network_check_seconds: int = 30
    portal_hosts: tuple[str, ...] = ('tmrsearch.ipindia.gov.in',)
    vpn_config_dir: Path | None = None
    # Tunnels the worker may rotate through. Empty keeps the single WORKER_VPN_CONFIG.
    # 'auto' in the environment means every reviewed .conf in the config directory.
    vpn_pool: tuple[str, ...] = ()
    # Portal accounts scraped at the same time (one login session each). 1 keeps the
    # original one-at-a-time behaviour; more needs that many accounts assigned to this worker.
    parallel_accounts: int = 1

    @classmethod
    def from_env(cls):
        base = os.environ.get('WORKER_COORDINATOR_URL', 'http://127.0.0.1:5010').rstrip('/')
        parsed = urlparse(base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid coordinator URL.')
        if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Remote coordinators must use HTTPS.')
        local_token = os.environ.get('WORKER_LOCAL_TOKEN', '')
        storage_dir = Path(os.environ.get('WORKER_STORAGE_DIR', '.runtime/worker')).resolve()
        # WORKER_TOKEN wins (existing machines). Otherwise reuse the credential saved after enrolling, or
        # enroll now with WORKER_ENROLL_KEY. The URL was validated above, so the key only ever travels over HTTPS
        # (or to localhost).
        from .enrollment import resolve_token
        token = resolve_token(os.environ, base, storage_dir)
        if min(len(token), len(local_token)) < 24:
            raise ValueError('Set WORKER_LOCAL_TOKEN (at least 24 characters) and either WORKER_TOKEN or WORKER_ENROLL_KEY.')
        mode = os.environ.get('WORKER_MODE', 'fixture')
        if mode not in ('fixture', 'live'):
            raise ValueError('WORKER_MODE must be fixture or live.')
        path = os.environ.get('WORKER_ACCOUNT_FILE')
        if mode == 'live' and not os.environ.get('WORKER_VPN_CONFIG'):
            raise ValueError('Live mode requires a WireGuard config name.')
        pool_value = os.environ.get('WORKER_VPN_POOL', '').strip()
        pool = ()
        if pool_value.lower() == 'auto':
            directory = Path(os.environ['WORKER_VPN_CONFIG_DIR']).resolve() if os.environ.get('WORKER_VPN_CONFIG_DIR') \
                else Path(__file__).resolve().parents[1] / 'vpn-configs'
            pool = tuple(sorted(p.stem for p in directory.glob('*.conf') if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', p.stem)))
        elif pool_value:
            pool = tuple(n.strip() for n in pool_value.split(',') if n.strip())
        return cls(base, token, local_token, storage_dir,
                   vpn_pool=pool, parallel_accounts=int(os.environ.get('WORKER_PARALLEL_ACCOUNTS', '1')),
                   port=int(os.environ.get('WORKER_PORT', '5011')), mode=mode,
                   batch_size=int(os.environ.get('WORKER_BATCH_SIZE', '5')),
                   batch_seconds=int(os.environ.get('WORKER_BATCH_SECONDS', '1200')),
                   vpn_config=os.environ.get('WORKER_VPN_CONFIG', ''), account_file=Path(path).resolve() if path else None,
                   max_pending=int(os.environ.get('WORKER_MAX_PENDING', '100')),
                   min_free_bytes=int(os.environ.get('WORKER_MIN_FREE_MB', '256')) * 1024 * 1024,
                   full_check_days=int(os.environ.get('WORKER_FULL_CHECK_DAYS', '30')),
                   network_mode=os.environ.get('WORKER_NETWORK_MODE', 'offline'),
                   network_check_seconds=int(os.environ.get('WORKER_NETWORK_CHECK_SECONDS', '30')),
                   portal_hosts=tuple(h.strip().lower() for h in os.environ.get('WORKER_PORTAL_HOSTS', 'tmrsearch.ipindia.gov.in').split(',') if h.strip()),
                   vpn_config_dir=Path(os.environ['WORKER_VPN_CONFIG_DIR']).resolve() if os.environ.get('WORKER_VPN_CONFIG_DIR') else None)

    def __post_init__(self):
        parsed = urlparse(self.coordinator_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Invalid coordinator URL.')
        if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Remote coordinators must use HTTPS.')
        if self.mode not in ('fixture', 'live'):
            raise ValueError('WORKER_MODE must be fixture or live.')
        if not 1 <= self.batch_size <= 100 or not 60 <= self.batch_seconds <= 86400:
            raise ValueError('Batch size must be 1-100 and duration 60-86400 seconds.')
        if not 1 <= self.max_pending <= 10000 or self.min_free_bytes < 0:
            raise ValueError('Invalid backlog or free-disk limit.')
        if not 1 <= self.full_check_days <= 365:
            raise ValueError('Full safety checks must be every 1-365 days.')
        if self.network_mode not in ('offline', 'split'):
            raise ValueError('WORKER_NETWORK_MODE must be offline or split.')
        if not 10 <= self.network_check_seconds <= 300:
            raise ValueError('Network check interval must be 10-300 seconds.')
        if not self.portal_hosts or any(not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', h) for h in self.portal_hosts):
            raise ValueError('Portal hosts must be reviewed DNS hostnames.')
        if not 1 <= self.parallel_accounts <= 8:
            raise ValueError('Parallel accounts must be 1-8.')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) for name in self.vpn_pool):
            raise ValueError('VPN pool names must be safe WireGuard config names without an extension.')
        if self.mode == 'live' and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.vpn_config):
            raise ValueError('Live mode requires a safe WireGuard config name without its extension.')
