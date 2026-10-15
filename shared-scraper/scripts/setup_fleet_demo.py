"""Create an isolated fixture demo without legacy app imports."""
import os
from pathlib import Path
import secrets
import sys

root = Path(__file__).parent.parent
sys.path.insert(0, str(root))
import psycopg
from psycopg import sql
from werkzeug.security import generate_password_hash
from cryptography.fernet import Fernet
from control_plane.config import Settings
from control_plane.repository import Repository
from migrations.__main__ import migrate
from migrations.prepare_legacy import prepare

if __name__ == '__main__':
    port = int(os.environ.get('FLEET_DEMO_PG_PORT', '55439'))
    administrator = os.environ.get('FLEET_DEMO_PG_USER', 'fleet_test_admin')
    database = 'fleet_dev_control'
    base = f'postgresql://{administrator}@127.0.0.1:{port}/'
    runtime = root.parent / '.runtime'
    runtime.mkdir(exist_ok=True)
    config = runtime / 'fleet-demo.env'
    if config.exists():
        raise SystemExit('Demo configuration already exists; reuse it instead of replacing credentials.')
    with psycopg.connect(base + 'postgres', autocommit=True) as conn:
        if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database,)).fetchone():
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    url = base + database
    prepare(url)
    migrate(url)
    admin_token, local_token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(18)
    artifacts = runtime / 'control-artifacts'
    repo = Repository(Settings(url, artifacts, admin_token, publish_legacy=True))
    worker = repo.create_worker('fixture-worker')
    with psycopg.connect(url) as conn:
        conn.execute('INSERT INTO users(email,password_hash,is_admin,is_active,display_name) VALUES(%s,%s,true,true,%s) '
                     'ON CONFLICT DO NOTHING', ('demo-admin@example.invalid', generate_password_hash(password), 'Demo Administrator'))
    values = {
        'CONTROL_ENV': 'development', 'CONTROL_DATABASE_URL': url, 'CONTROL_ADMIN_TOKEN': admin_token,
        'CONTROL_ACCOUNT_KEY': Fernet.generate_key().decode(), 'CONTROL_ARTIFACT_DIR': str(artifacts), 'CONTROL_PUBLISH_LEGACY': '1',
        'TM_ENV': 'development', 'TM_LOAD_LEGACY_ENV': '0', 'TM_DATABASE_URL': url, 'TM_FLEET_CONTROL_ENABLED': '1',
        'TM_AUTO_MIGRATE': '0', 'TM_BACKGROUND_TASKS_ENABLED': '0', 'COOKIE_SECURE': '0', 'USE_WAITRESS': '1',
        'WORKER_COORDINATOR_URL': 'http://127.0.0.1:5010', 'WORKER_TOKEN': worker['token'], 'WORKER_LOCAL_TOKEN': local_token,
        'WORKER_STORAGE_DIR': str(runtime / 'demo-worker'), 'WORKER_MODE': 'fixture',
        'SCRAPER_LOAD_LEGACY_ENV': '0', 'SCRAPER_EXTERNAL_VPN_CONTROL': '1',
        'SECRET_KEY': secrets.token_hex(32), 'SENTRY_DSN_BACKEND': '',
        'DEMO_ADMIN_EMAIL': 'demo-admin@example.invalid', 'DEMO_ADMIN_PASSWORD': password,
    }
    config.write_text('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n', encoding='utf-8')
    try:
        config.chmod(0o600)
    except OSError:
        pass
    print('Created isolated fixture demo. Credentials are in:', config)
    print('Start from workspace root: .\\run-all.ps1 -Mode demo')
