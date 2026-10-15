"""Root-only staging preparation. Preserves the live database and services.

Uses host psql/pg_dump/pg_restore through the postgres OS account. Never prints
credentials. Reruns do not overwrite staging or restore over an existing DB.
"""
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile

RELEASE = Path(__file__).resolve().parents[2]
STATE = Path('/etc/tmtracker/staging-state.json')
DB = 'fleet_staging_datascraper'
ROLES = ('tm_stage_owner', 'tm_stage_control', 'tm_stage_user')
PRESERVED_TABLES = ('users', 'my_trademarks', 'tma_documents', 'ipindia_reminders',
                    'ipindia_correspondence', 'compliance_status', 'tm_status_history',
                    'custom_tasks', 'subscriptions', 'plans')


def psql(database, query):
    result = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-X', '-q', '-A', '-t',
                             '-v', 'ON_ERROR_STOP=1', '-d', database], input=query,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('PostgreSQL preparation failed. Credentials were not printed; live database unchanged.')
    return result.stdout.strip()


def read_env(path):
    output = {}
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            output.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return output


def fingerprints():
    result = {}
    for table in PRESERVED_TABLES:
        row = "md5((to_jsonb(t)-'fleet_canonical'-'fleet_sha256')::text)"
        result[table] = json.loads(psql(DB, f"SELECT json_build_object('count',count(*),'fingerprint',md5(coalesce(string_agg({row},'' ORDER BY {row}),''))) FROM public.{table} t;"))
    return result


def private_write(path, text):
    with path.open('x', encoding='utf-8') as handle:
        os.chmod(path, 0o600)
        handle.write(text)


def write_env(path, values):
    for key, value in values.items():
        if not key.replace('_', '').isalnum() or any(c in str(value) for c in '\r\n\0'):
            raise ValueError('Invalid configuration entry; no configuration values printed.')
    # env_file's raw format prevents dollar interpolation in inherited settings.
    private_write(path, ''.join(f'{key}={value}\n' for key, value in values.items()))


def main():
    import pwd
    if os.geteuid() != 0:
        raise RuntimeError('Run on Contabo as root.')
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state.get('prepared'):
            print('Staging already prepared. Reusing its private configuration.')
            return
        raise RuntimeError('A previous preparation did not finish. Do not delete its database or state; inspect before resuming.')
    account = pwd.getpwnam('tmtracker')
    required = [Path('/opt/tmtracker/.env'), Path('/opt/tmtracker/tm_manager/.env'),
                Path('/opt/tmtracker/tm_manager/Journal'), Path('/opt/tmtracker/data')]
    if not all(path.exists() and not path.is_symlink() for path in required):
        raise RuntimeError('Expected live application paths are missing or symlinked; stop for review.')
    version = psql('datascraper', 'SHOW server_version;')
    if not version.startswith('16.'):
        raise RuntimeError('Expected the audited PostgreSQL 16 server.')
    if psql('postgres', f"SELECT 1 FROM pg_database WHERE datname='{DB}';"):
        raise RuntimeError('Staging database already exists; it will not be overwritten.')
    names = ','.join("'" + role + "'" for role in ROLES)
    if psql('postgres', f'SELECT rolname FROM pg_roles WHERE rolname IN ({names});'):
        raise RuntimeError('Staging roles already exist; they will not be overwritten.')
    size = int(psql('datascraper', "SELECT pg_database_size('datascraper');"))
    if shutil.disk_usage('/').free < size * 3 + 6 * 1024**3:
        raise RuntimeError('Insufficient free disk for backup, staging restore and application image.')
    for filename in ('staging-migration.env', 'staging-control.env', 'staging-user.env', 'staging-compose.env'):
        if (STATE.parent / filename).exists():
            raise RuntimeError('Existing staging configuration found; will not replace it.')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    backup_root = Path('/root/tmtracker-backups')
    if backup_root.is_symlink():
        raise RuntimeError('Backup folder is symlinked; stop for review.')
    backup_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backup_root, 0o700)
    backup = backup_root / stamp
    backup.mkdir(mode=0o700)
    print('Creating private database backup:', backup)
    with (backup / 'datascraper.dump').open('xb') as handle:
        subprocess.run(['sudo', '-u', 'postgres', 'pg_dump', '-Fc', '-d', 'datascraper'],
                       stdout=handle, check=True)
    os.chmod(backup / 'datascraper.dump', 0o600)
    subprocess.run(['pg_restore', '--list', str(backup / 'datascraper.dump')],
                   stdout=subprocess.DEVNULL, check=True)
    with tarfile.open(backup / 'configuration.tar.gz', 'w:gz') as archive:
        for path in required[:2] + [Path('/etc/nginx'), Path('/etc/letsencrypt')]:
            if path.exists():
                archive.add(path, arcname=str(path).lstrip('/'))
        for path in sorted(Path('/etc/systemd/system').glob('tmtracker*.service')):
            archive.add(path, arcname=str(path).lstrip('/'))
    os.chmod(backup / 'configuration.tar.gz', 0o600)
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    passwords = {role: secrets.token_urlsafe(36) for role in ROLES}
    state = {'prepared': False, 'database': DB, 'backup': str(backup), 'release': str(RELEASE)}
    private_write(STATE, json.dumps(state))
    # Generated URL-safe passwords contain no quotes. SQL never goes in argv.
    commands = ['BEGIN;'] + [f"CREATE ROLE {role} LOGIN PASSWORD '{passwords[role]}' NOSUPERUSER NOCREATEDB NOCREATEROLE;" for role in ROLES] + ['COMMIT;']
    psql('postgres', '\n'.join(commands))
    psql('postgres', f'CREATE DATABASE {DB} OWNER tm_stage_owner;')
    # Root owns the private backup; postgres reads it through stdin, not a
    # world-readable file or recursive permission change.
    with (backup / 'datascraper.dump').open('rb') as handle:
        subprocess.run(['sudo', '-u', 'postgres', 'pg_restore', '--exit-on-error', '--single-transaction',
                        '--clean', '--if-exists', '--no-owner', '--no-privileges', '--role=tm_stage_owner', '-d', DB],
                       stdin=handle, check=True)
    print('Staging restore completed on PostgreSQL', version)
    counts = psql(DB, "SELECT json_build_object('users',(SELECT count(*) FROM users),'portfolios',(SELECT count(*) FROM my_trademarks),'tma_documents',(SELECT count(*) FROM tma_documents),'reminders',(SELECT count(*) FROM ipindia_reminders),'correspondence',(SELECT count(*) FROM ipindia_correspondence));")
    private_write(STATE.parent / 'staging-before-counts.json', counts + '\n')
    private_write(STATE.parent / 'staging-before-fingerprints.json', json.dumps(fingerprints()))
    uid, gid = account.pw_uid, account.pw_gid
    Path('/var/lib/tmtracker').mkdir(mode=0o755, exist_ok=True)
    staging = Path('/var/lib/tmtracker/staging')
    staging.mkdir(mode=0o750, exist_ok=True)
    os.chown(staging, uid, gid)
    for name in ('fleet-artifacts', 'logo-cache', 'journal-logo-cache'):
        directory = Path('/var/lib/tmtracker/staging') / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chown(directory, uid, gid)
    control = {'CONTROL_ENV': 'production',
               'CONTROL_DATABASE_URL': f'postgresql://tm_stage_control:{passwords["tm_stage_control"]}@127.0.0.1:5432/{DB}',
               'CONTROL_ADMIN_TOKEN': secrets.token_urlsafe(36),
               'CONTROL_ACCOUNT_KEY': __import__('base64').urlsafe_b64encode(secrets.token_bytes(32)).decode(),
               'CONTROL_ARTIFACT_DIR': '/var/lib/tmtracker/staging/fleet-artifacts',
               'CONTROL_PUBLISH_LEGACY': '1'}
    migration = {**control, 'CONTROL_DATABASE_URL': f'postgresql://tm_stage_owner:{passwords["tm_stage_owner"]}@127.0.0.1:5432/{DB}'}
    user = read_env(required[1])
    # Inherited DB settings are replaced as well as the fleet connection URL.
    user.update({'PG_HOST': '127.0.0.1', 'PG_PORT': '5432', 'PG_DBNAME': DB,
                 'PG_USER': 'tm_stage_user', 'PG_PASSWORD': passwords['tm_stage_user'],
                 'TM_DATABASE_URL': f'postgresql://tm_stage_user:{passwords["tm_stage_user"]}@127.0.0.1:5432/{DB}',
                 'TM_LOAD_LEGACY_ENV': '0', 'TM_FLEET_CONTROL_ENABLED': '1',
                 'TM_AUTO_MIGRATE': '0', 'TM_BACKGROUND_TASKS_ENABLED': '0',
                 'TM_BIND_HOST': '127.0.0.1', 'SECRET_KEY': secrets.token_urlsafe(36),
                 'TM_CONTROL_API_BASE': 'http://127.0.0.1:5510',
                 'TM_CONTROL_ADMIN_TOKEN': control['CONTROL_ADMIN_TOKEN'],
                 'COOKIE_SECURE': '0', 'SENTRY_DSN_BACKEND': '',
                 'OPS_API_USER': '', 'OPS_API_PASS': '', 'BASIC_AUTH_USER': '', 'BASIC_AUTH_PASS': '',
                 'DIGEST_EMAIL_ADDRESS': '', 'DIGEST_EMAIL_APP_PASSWORD': '',
                 'SCRAPER_API_BASE_URL': 'http://127.0.0.1:9',
                 'RATE_LIMIT_STORAGE_URI': 'memory://'})
    write_env(STATE.parent / 'staging-migration.env', migration)
    write_env(STATE.parent / 'staging-control.env', control)
    write_env(STATE.parent / 'staging-user.env', user)
    write_env(STATE.parent / 'staging-compose.env', {'TMTRACKER_UID': str(uid), 'TMTRACKER_GID': str(gid), 'RELEASE_TAG': 'contabo-staging'})
    state['prepared'] = True
    STATE.write_text(json.dumps(state), encoding='utf-8')
    print('Staging prepared. Credentials saved privately; live data and services unchanged.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Preparation stopped:', str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__)
        raise SystemExit(1)
