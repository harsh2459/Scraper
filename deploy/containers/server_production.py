"""Root-only prepare/activate/rollback for the audited Contabo installation.

Preparation leaves the public app running. Activation freezes the old services,
takes a fresh backup, migrates the live DB and switches the existing TLS site.
Rollback restores services/routes, never silently restores an older database.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

from prepare_server_staging import psql, read_env, write_env, private_write, PRESERVED_TABLES
from production_nginx import update

RELEASE = Path(__file__).resolve().parents[2]
STATE = Path('/etc/tmtracker/live-state.json')
DB = 'datascraper'
SERVICES = ('tmtracker-backend', 'tmtracker-scraper', 'tmtracker-pdf-scraper')
COMPOSE_ENV = STATE.parent / 'live-compose.env'
ARTIFACTS = Path('/var/lib/tmtracker/live/fleet-artifacts')


def run(args, *, input=None, capture=False):
    # Never expose subprocess output on failures: config/SQL/provider exceptions
    # can include credentials. Docker build output is safe and useful.
    result = subprocess.run(args, input=input, text=True, capture_output=True)
    if result.returncode:
        # Retain diagnostics privately rather than discard the cause. Provider
        # or database output must never be displayed in the terminal/chat.
        diagnostic = STATE.parent / 'production-last-error.log'
        diagnostic.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with diagnostic.open('w', encoding='utf-8') as handle:
            os.chmod(diagnostic,0o600)
            handle.write(result.stdout + '\n' + result.stderr)
        raise RuntimeError('Command failed: ' + Path(args[0]).name + '. Private diagnostics: ' + str(diagnostic))
    return result.stdout if capture else None


def save(state):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(STATE)


def compose(*args, capture=False):
    return run(['docker','compose','--env-file',str(COMPOSE_ENV),'-f',
                str(RELEASE / 'deploy/containers/compose.production.yaml'),*args], capture=capture)


def fingerprints():
    output = {}
    for table in PRESERVED_TABLES:
        expression = "md5((to_jsonb(t)-'fleet_canonical'-'fleet_sha256')::text)"
        output[table] = json.loads(psql(DB, f"SELECT json_build_object('count',count(*),'fingerprint',md5(coalesce(string_agg({expression},'' ORDER BY {expression}),''))) FROM public.{table} t;"))
    return output


def nginx_plan(frontend):
    found = []
    for link in Path('/etc/nginx/sites-enabled').iterdir():
        target = link.resolve()
        if not target.is_relative_to(Path('/etc/nginx')) or not target.is_file():
            raise RuntimeError('Unexpected Nginx site path; stop for review.')
        text = target.read_text()
        if 'mytmtracker.com' not in text:
            continue
        try:
            proposed = update(text, frontend)
        except ValueError as exc:
            # A separate scraper vhost has no main-domain HTTPS server.
            if 'Expected one main-domain' in str(exc):
                continue
            raise
        found.append((target,text,proposed))
    if len(found) != 1:
        raise RuntimeError('Expected one enabled main-domain HTTPS configuration.')
    return found[0]


def write_replacement(path, text):
    info = path.stat()
    temp = path.with_name(path.name + '.tmtracker-new')
    if temp.exists() or temp.is_symlink():
        raise RuntimeError('A previous Nginx replacement file exists; inspect it first.')
    with temp.open('x') as handle:
        handle.write(text)
    os.chmod(temp, info.st_mode & 0o777)
    os.chown(temp, info.st_uid, info.st_gid)
    temp.replace(path)


def prepare():
    import pwd
    manifest = json.loads((RELEASE / 'release-manifest.json').read_text())
    if manifest.get('kind') != 'production':
        raise RuntimeError('This is not the production release archive.')
    run(['python3', str(RELEASE / 'deploy/containers/verify_release.py')])
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state.get('release') != str(RELEASE):
            raise RuntimeError('A different production release is already recorded; stop for review.')
        if state.get('phase') == 'prepared':
            print('Production preparation already passed. Ready for activate.')
            return
        if state.get('phase') == 'configured':
            finish_prepare(state)
            return
        raise RuntimeError('Production state already exists. Inspect or use rollback; no overwrite.')
    if not psql(DB, 'SHOW server_version;').startswith('16.'):
        raise RuntimeError('Expected the audited PostgreSQL 16 database.')
    if not Path('/opt/tmtracker/.venv/bin/python').exists():
        raise RuntimeError('Expected the original application Python environment.')
    for port in (5005,5010):
        if port == 5010:
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1',port)) == 0:
                    raise RuntimeError('Production coordinator port is already occupied.')
    keys = ('GOOGLE_DRIVE_CLIENT_ID','GOOGLE_DRIVE_CLIENT_SECRET','GOOGLE_DRIVE_REFRESH_TOKEN')
    account = pwd.getpwnam('tmtracker')
    user = read_env(Path('/opt/tmtracker/tm_manager/.env'))
    tag = dt.datetime.now(dt.timezone.utc).strftime('live-%Y%m%dT%H%M%S')
    frontend = '/var/www/tmtracker-live/' + tag
    target, original, proposed = nginx_plan(frontend)
    if shutil.disk_usage('/').free < int(psql(DB,"SELECT pg_database_size('datascraper');")) * 2 + 4*1024**3:
        raise RuntimeError('Insufficient space for a fresh backup and production image.')
    roles = ('tm_live_control','tm_live_user')
    if psql('postgres', "SELECT rolname FROM pg_roles WHERE rolname IN ('tm_live_control','tm_live_user');"):
        raise RuntimeError('Production roles already exist; will not reset their passwords.')
    STATE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('live-control.env','live-user.env','live-compose.env'):
        if (STATE.parent / name).exists():
            raise RuntimeError('Production configuration already exists; will not overwrite it.')
    state = {'phase':'preparing','release':str(RELEASE),'tag':tag,'frontend':frontend,
             'nginx':str(target),'nginx_sha256':hashlib.sha256(original.encode()).hexdigest(),
             'services':{name: {'active':subprocess.run(['systemctl','is-active',name],capture_output=True,text=True).stdout.strip(),
                                'enabled':subprocess.run(['systemctl','is-enabled',name],capture_output=True,text=True).stdout.strip()} for name in SERVICES}}
    save(state)
    private_write(STATE.parent / 'live-nginx-before.conf', original)
    private_write(STATE.parent / 'live-nginx-after.conf', proposed)
    passwords = {role:secrets.token_urlsafe(36) for role in roles}
    psql('postgres','BEGIN;\n' + '\n'.join(f"CREATE ROLE {role} LOGIN PASSWORD '{passwords[role]}' NOSUPERUSER NOCREATEDB NOCREATEROLE;" for role in roles) + '\nCOMMIT;')
    for directory in (Path('/var/lib/tmtracker'),ARTIFACTS.parent,ARTIFACTS):
        if directory.is_symlink():
            raise RuntimeError('Unexpected linked artifact directory.')
        directory.mkdir(parents=True,exist_ok=True,mode=0o750 if directory != Path('/var/lib/tmtracker') else 0o755)
        if directory != Path('/var/lib/tmtracker'):
            os.chown(directory,account.pw_uid,account.pw_gid)
    for name in ('data','tm_manager/Journal','tm_manager/_logo_cache','tm_manager/_journal_logo_cache'):
        directory = Path('/opt/tmtracker') / name
        if directory.is_symlink():
            raise RuntimeError('Unexpected linked legacy data directory.')
        if not directory.exists():
            if name in ('data','tm_manager/Journal'):
                raise RuntimeError('Historical data directory is missing.')
            directory.mkdir(mode=0o750)
            os.chown(directory,account.pw_uid,account.pw_gid)
    control = {'CONTROL_ENV':'production','CONTROL_PORT':'5010','CONTROL_PUBLISH_LEGACY':'1',
               'CONTROL_DATABASE_URL':f'postgresql://tm_live_control:{passwords["tm_live_control"]}@127.0.0.1:5432/datascraper',
               'CONTROL_ADMIN_TOKEN':secrets.token_urlsafe(36),
               'CONTROL_ACCOUNT_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
               'CONTROL_ARTIFACT_DIR':str(ARTIFACTS),'CONTROL_STORAGE_MODE':'local'}
    secret = user.get('SECRET_KEY')
    if not secret:
        stored = Path('/opt/tmtracker/tm_manager/.secret_key')
        secret = stored.read_text().strip() if stored.is_file() else secrets.token_urlsafe(36)
    user.update({**{k:'' for k in keys}, 'GOOGLE_DRIVE_ROOT_FOLDER_ID':'',
                 'PG_HOST':'127.0.0.1','PG_PORT':'5432','PG_DBNAME':'datascraper','PG_USER':'tm_live_user','PG_PASSWORD':passwords['tm_live_user'],
                 'TM_DATABASE_URL':f'postgresql://tm_live_user:{passwords["tm_live_user"]}@127.0.0.1:5432/datascraper',
                 'TM_LOAD_LEGACY_ENV':'0','TM_FLEET_CONTROL_ENABLED':'1','TM_AUTO_MIGRATE':'0','TM_BACKGROUND_TASKS_ENABLED':'1',
                 'TM_BIND_HOST':'127.0.0.1','TM_MANAGER_PORT':'5005','USE_WAITRESS':'1','COOKIE_SECURE':'1','SECRET_KEY':secret,
                 'TM_CONTROL_API_BASE':'http://127.0.0.1:5010','TM_CONTROL_ADMIN_TOKEN':control['CONTROL_ADMIN_TOKEN'],
                 'CONTROL_ARTIFACT_DIR':str(ARTIFACTS),'OPS_API_USER':'','OPS_API_PASS':'','BASIC_AUTH_USER':'','BASIC_AUTH_PASS':'',
                 'SCRAPER_API_BASE_URL':'http://127.0.0.1:9'})
    write_env(STATE.parent / 'live-control.env',control)
    write_env(STATE.parent / 'live-user.env',user)
    write_env(COMPOSE_ENV,{'TMTRACKER_UID':str(account.pw_uid),'TMTRACKER_GID':str(account.pw_gid),'RELEASE_TAG':tag})
    state['phase'] = 'configured'
    save(state)
    finish_prepare(state)


def finish_prepare(state):
    frontend, tag = state['frontend'], state['tag']
    compose('config','--quiet')
    print('Rehearsing release migrations against the existing separate staging database.',flush=True)
    run(['sudo','-u','postgres','/opt/tmtracker/.venv/bin/python',str(RELEASE/'deploy/containers/rehearse_server_production.py')])
    print('Building production image. Existing public application remains running.',flush=True)
    result = subprocess.run(['docker','compose','--env-file',str(COMPOSE_ENV),'-f',str(RELEASE/'deploy/containers/compose.production.yaml'),'build'])
    if result.returncode:
        raise RuntimeError('Production image build failed; old application remains running.')
    container = run(['docker','create','tmtracker-live:'+tag],capture=True).strip()
    try:
        Path(frontend).mkdir(parents=True,mode=0o755)
        run(['docker','cp',container+':/opt/tmtracker/tm_manager/frontend/dist/.',frontend+'/'])
        run(['chmod','-R','a+rX',frontend])
    finally:
        run(['docker','rm',container])
    if not (Path(frontend)/'index.html').is_file():
        raise RuntimeError('Production frontend is missing.')
    state['phase'] = 'prepared'
    save(state)
    print('Production image prepared with local document storage. Public website unchanged. Next: activate.')


def readiness():
    env = read_env(STATE.parent / 'live-control.env')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(30):
        try:
            request = urllib.request.Request('http://127.0.0.1:5010/api/control/v1/admin/overview',headers={'Authorization':'Bearer '+env['CONTROL_ADMIN_TOKEN']})
            with opener.open(request,timeout=3) as response:
                json.load(response)
            try:
                opener.open('http://127.0.0.1:5005/api/me',timeout=3).close()
                raise RuntimeError('Anonymous user request unexpectedly succeeded.')
            except urllib.error.HTTPError as response:
                with response:
                    if response.code != 401 or json.load(response).get('error') != 'auth required':
                        raise ValueError('Authentication check failed.')
            return
        except (urllib.error.URLError,TimeoutError,ValueError):
            time.sleep(2)
    raise RuntimeError('Production service readiness failed.')


def rollback(state):
    compose('down')
    target = Path(state['nginx'])
    current = target.read_text()
    before = (STATE.parent/'live-nginx-before.conf').read_text()
    after = (STATE.parent/'live-nginx-after.conf').read_text()
    if current not in (before,after):
        raise RuntimeError('Nginx has concurrent changes; automatic rollback will not overwrite them.')
    if current != before:
        write_replacement(target,before)
        run(['nginx','-t'])
        run(['systemctl','reload','nginx'])
    for name,old in state['services'].items():
        if old['enabled'] == 'enabled':
            run(['systemctl','enable',name])
        if old['active'] in ('active','activating','reloading'):
            run(['systemctl','start',name])
    state['phase'] = 'rolled-back'
    save(state)
    print('Old application and routes restored. Live DB and files retained; no automatic database restore.')


def activate(state):
    if state.get('phase') not in ('prepared','rolled-back'):
        raise RuntimeError('Complete production prepare first; activation will not repeat a partial migration.')
    target = Path(state['nginx'])
    if hashlib.sha256(target.read_bytes()).hexdigest() != state['nginx_sha256']:
        raise RuntimeError('Nginx changed since preparation; stop for review.')
    run(['nginx','-t'])
    compose('config','--quiet')
    print('Starting maintenance window: freezing old services and creating a fresh private backup.',flush=True)
    state['phase'] = 'activating'
    save(state)
    try:
        run(['systemctl','stop',*SERVICES])
        # Every activation attempt gets a fresh backup; retain failed attempts.
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup = Path('/root/tmtracker-backups')/(state['tag']+'-'+stamp)
        backup.mkdir(parents=True,mode=0o700)
        backup.parent.chmod(0o700)
        state['backup'] = str(backup)
        state.setdefault('backups',[]).append(str(backup))
        save(state)
        with (backup/'datascraper.dump').open('xb') as handle:
            result = subprocess.run(['sudo','-u','postgres','pg_dump','-Fc','-d',DB],stdout=handle,stderr=subprocess.PIPE)
        (backup/'datascraper.dump').chmod(0o600)
        if result.returncode:
            raise RuntimeError('Fresh database backup failed.')
        run(['pg_restore','--list',str(backup/'datascraper.dump')])
        with tarfile.open(backup/'configuration.tar.gz','w:gz') as archive:
            for path in [Path('/etc/nginx'),Path('/etc/letsencrypt'),Path('/opt/tmtracker/.env'),Path('/opt/tmtracker/tm_manager/.env')]:
                archive.add(path,arcname=str(path).lstrip('/'))
            for path in Path('/etc/systemd/system').glob('tmtracker*.service'):
                archive.add(path,arcname=str(path).lstrip('/'))
        (backup/'configuration.tar.gz').chmod(0o600)
        before = fingerprints()
        private_write(backup/'before-fingerprints.json',json.dumps(before))
        run(['sudo','-u','postgres','/opt/tmtracker/.venv/bin/python',str(RELEASE/'deploy/containers/migrate_live.py')])
        if before != fingerprints():
            raise RuntimeError('Original-record verification failed after migration. Backup retained; inspect before database restoration.')
        # Same reviewed grants as staging, with production role names.
        grants = (RELEASE/'deploy/fleet-service-grants.sql').read_text().replace('tm_user_service','tm_live_user').replace('tm_control_service','tm_live_control')
        from finish_server_staging import EXTRA_GRANTS
        psql(DB,'BEGIN;\n'+grants+EXTRA_GRANTS.replace('tm_stage_user','tm_live_user')+'\nCOMMIT;')
        compose('up','-d','control','user')
        readiness()
        write_replacement(target,(STATE.parent/'live-nginx-after.conf').read_text())
        run(['nginx','-t'])
        run(['systemctl','reload','nginx'])
        for path in ('/','/admin/'):
            run(['curl','--fail','--silent','--show-error','--max-time','10','--resolve','mytmtracker.com:443:127.0.0.1','https://mytmtracker.com'+path])
        run(['systemctl','disable',*SERVICES])
        state['phase'] = 'live'
        save(state)
        print('LIVE: https://mytmtracker.com and https://mytmtracker.com/admin/ . Original users/database/documents retained.')
        print('New workers still need provisioning and a real VPN/scrape pilot. Historical PDF cleanup has not run.')
    except Exception:
        print('Activation failed. Restoring the old application and routes.',flush=True)
        rollback(state)
        raise


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run on Contabo as root.')
    parser = argparse.ArgumentParser()
    parser.add_argument('operation',choices=('prepare','activate','rollback'))
    args = parser.parse_args()
    if args.operation == 'prepare':
        prepare()
    else:
        state = json.loads(STATE.read_text())
        if state['release'] != str(RELEASE):
            raise RuntimeError('Run from the production release recorded in live-state.json.')
        if args.operation == 'activate':
            activate(state)
        else:
            if state.get('phase') not in ('live','activating','rolled-back'):
                raise RuntimeError('No activated production deployment to roll back.')
            rollback(state)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Production stopped:',str(exc) if isinstance(exc,(RuntimeError,ValueError)) else type(exc).__name__)
        raise SystemExit(1)
