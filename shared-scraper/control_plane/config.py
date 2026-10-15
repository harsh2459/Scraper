"""Explicit service configuration; never loads legacy .env files."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_dir: Path
    admin_token: str
    port: int = 5010
    reservation_seconds: int = 1800
    grace_seconds: int = 300
    max_attempts: int = 5
    max_upload_bytes: int = 32 * 1024 * 1024
    publish_legacy: bool = False
    account_key: str = ''
    storage_mode: str = 'local'
    drive_options: dict | None = None

    def __post_init__(self):
        if self.storage_mode not in ('local', 'drive'):
            raise ValueError('Invalid artifact storage mode.')
        if self.storage_mode == 'drive' and not all((self.drive_options or {}).get(k) for k in ('client_id','client_secret','refresh_token','root_folder_id')):
            raise ValueError('Drive mode requires explicit credentials and root folder.')
        if not 300 <= self.reservation_seconds <= 86400 or not 0 <= self.grace_seconds <= 3600:
            raise ValueError('Reservation must be 300-86400 seconds; grace must be 0-3600 seconds.')
        if not 1 <= self.max_attempts <= 100 or self.max_upload_bytes <= 0:
            raise ValueError('Invalid retry/upload limits.')
        if self.account_key:
            from cryptography.fernet import Fernet
            Fernet(self.account_key.encode())

    @classmethod
    def from_env(cls):
        url = os.environ.get('CONTROL_DATABASE_URL', '')
        if not url:
            raise ValueError('Set CONTROL_DATABASE_URL explicitly; legacy database credentials are never inherited.')
        parsed = urlparse(url)
        production = os.environ.get('CONTROL_ENV') == 'production'
        if not production and (parsed.hostname not in ('localhost', '127.0.0.1', '::1')
                               or not (parsed.path.startswith('/fleet_dev') or parsed.path.startswith('/fleet_test'))):
            raise ValueError('Development requires a loopback fleet_dev/fleet_test database.')
        token = os.environ.get('CONTROL_ADMIN_TOKEN', '')
        if len(token) < 24:
            raise ValueError('CONTROL_ADMIN_TOKEN must contain at least 24 characters.')
        return cls(url, Path(os.environ.get('CONTROL_ARTIFACT_DIR', '.runtime/control-artifacts')).resolve(), token,
                   port=int(os.environ.get('CONTROL_PORT', '5010')),
                   reservation_seconds=int(os.environ.get('CONTROL_RESERVATION_SECONDS', '1800')),
                   grace_seconds=int(os.environ.get('CONTROL_GRACE_SECONDS', '300')),
                   max_attempts=int(os.environ.get('CONTROL_MAX_ATTEMPTS', '5')),
                   max_upload_bytes=int(os.environ.get('CONTROL_MAX_UPLOAD_MB', '32')) * 1024 * 1024,
                   publish_legacy=os.environ.get('CONTROL_PUBLISH_LEGACY', '0') == '1',
                   account_key=os.environ.get('CONTROL_ACCOUNT_KEY', ''),
                   storage_mode=os.environ.get('CONTROL_STORAGE_MODE', 'local'),
                   drive_options={k: os.environ.get('GOOGLE_DRIVE_' + k.upper(), '') for k in ('client_id','client_secret','refresh_token','root_folder_id')})
