"""Import existing OTP accounts through the live coordinator, without printing secrets.

Run as root on Contabo. Uses peer psql for source reads; the coordinator encrypts
passwords with its actual runtime key. Existing fleet accounts are not overwritten.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:5010/api/control/v1/admin'


class ImportStopped(RuntimeError):
    pass


def env_values(path):
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key,value = line.split('=',1)
            values.setdefault(key.strip(),value.strip().strip('"').strip("'"))
    return values


class API:
    def __init__(self, token):
        self.token = token
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def call(self, method, path, body=None):
        request = urllib.request.Request(BASE+path,method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with self.opener.open(request,timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise ImportStopped('Coordinator API returned HTTP '+str(code)+'. No credentials were printed.') from None
        except Exception:
            raise ImportStopped('Coordinator request failed. No credentials were printed; rerunning skips successful imports.') from None

    def collection(self, name):
        output = []
        while True:
            reply = self.call('GET',f'/{name}?limit=100&offset={len(output)}')
            items = reply['items']
            output.extend(items)
            if not items or len(output) >= reply['total']:
                return output


def find_api(explicit=None):
    tokens = []
    # Read only the process listening on the coordinator's loopback port.
    result = subprocess.run(['ss','-ltnp','sport = :5010'],capture_output=True,text=True)
    for pid in set(re.findall(r'pid=(\d+)',result.stdout)):
        try:
            values = dict(item.split(b'=',1) for item in (Path('/proc')/pid/'environ').read_bytes().split(b'\0') if b'=' in item)
            token = values.get(b'CONTROL_ADMIN_TOKEN',b'').decode()
            if token:
                tokens.append(token)
        except (OSError,ValueError):
            pass
    paths = [explicit] if explicit else [Path('/etc/tmtracker/control.env'),Path('/etc/tmtracker/live-control.env'),
        Path('/opt/tmtracker-drive-job/.env'),Path('/opt/tmtracker/.env')]
    for path in paths:
        if path and path.is_file():
            token = env_values(path).get('CONTROL_ADMIN_TOKEN')
            if token:
                tokens.append(token)
    for token in dict.fromkeys(tokens):
        api = API(token)
        try:
            api.call('GET','/overview')
            return api
        except ImportStopped:
            pass
    raise ImportStopped('Could not authenticate to the running coordinator. Supply --control-env with its private env file path; do not paste its token.')


def read_legacy():
    query = "SELECT coalesce(json_agg(to_jsonb(t) ORDER BY id),'[]'::json) FROM public.otp_email_pool t;"
    result = subprocess.run(['sudo','-u','postgres','psql','-X','-q','-A','-t','-v','ON_ERROR_STOP=1','-d','datascraper'],
                            input=query,capture_output=True,text=True)
    if result.returncode:
        raise ImportStopped('Could not read the original account pool; no data changed.')
    return json.loads(result.stdout)


def import_accounts(api, rows, assign_first_to=None):
    workers = api.collection('workers') if assign_first_to else []
    worker = None
    if assign_first_to:
        matches = [w for w in workers if w['name']==assign_first_to and w['status']!='retired']
        if len(matches)!=1:
            raise ImportStopped('Expected exactly one non-retired worker with the requested name.')
        worker = matches[0]
    existing = {row['email'].strip().casefold():row for row in api.collection('accounts')}
    added = skipped = 0
    for row in rows:
        email = row['email_address'].strip().casefold()
        if email in existing:
            skipped += 1
            continue
        reply = api.call('POST','/accounts',{'email':email,'app_password':row['app_password'],
            'imap_server':row.get('imap_server') or 'imap.gmail.com','imap_port':row.get('imap_port') or 993})
        enabled = bool(row.get('enabled',True))
        if not enabled:
            api.call('PATCH','/accounts/'+reply['id'],{'worker_id':None,'enabled':False})
        existing[email] = {'id':reply['id'],'email':email,'enabled':enabled,'worker_id':None}
        added += 1
    assigned = False
    if worker and not any(a.get('worker_id')==worker['id'] and a['enabled'] for a in existing.values()):
        for row in rows:
            account = existing[row['email_address'].strip().casefold()]
            if account['enabled'] and not account.get('worker_id') and row.get('reserved_for') not in ('warm','warm_pool','wordmark','wordmark_pool') and not row.get('claimed_by_job_id'):
                api.call('PATCH','/accounts/'+account['id'],{'worker_id':worker['id'],'enabled':True})
                assigned = True
                break
        if not assigned:
            raise ImportStopped(f'Imported {added}, skipped {skipped}. No enabled, unassigned, idle cold account is available for the worker.')
    print(f'Accounts imported: {added}; already present: {skipped}. Original pool unchanged.')
    if worker:
        print('Worker account: assigned one available cold account.' if assigned else 'Worker already has an enabled account; assignment unchanged.')


if __name__ == '__main__':
    try:
        if os.geteuid()!=0:
            raise ImportStopped('Run this helper on Contabo as root.')
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--control-env',type=Path)
        parser.add_argument('--assign-first-to')
        args = parser.parse_args()
        import_accounts(find_api(args.control_env),read_legacy(),args.assign_first_to)
    except Exception as exc:
        print('Import stopped:',str(exc) if isinstance(exc,ImportStopped) else type(exc).__name__)
        raise SystemExit(1)
