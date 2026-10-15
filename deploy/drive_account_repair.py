"""Authorize locally, then apply the private authorization bundle on Contabo.

Never prints credentials, changes document IDs, uploads files or deletes files.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile

KEYS = ('GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET', 'GOOGLE_DRIVE_REFRESH_TOKEN')
SCOPE = 'https://www.googleapis.com/auth/drive.file'


class RepairError(Exception):
    pass


def read_env(path):
    values = {}
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and '=' in line:
            key, _, value = line.partition('=')
            # Match the application's first-value-wins config loader.
            values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def replace_auth(text, values):
    for key in KEYS:
        value = values.get(key)
        if not isinstance(value, str) or not value or any(c in value for c in '\r\n\x00'):
            raise RepairError('Incomplete or invalid authorization bundle.')
    output = []
    for line in text.splitlines():
        stripped = line.strip()
        key = stripped.split('=', 1)[0].strip()
        if stripped.startswith('#') or key not in KEYS:
            output.append(line)
    output.extend(key + '=' + values[key] for key in KEYS)
    return '\n'.join(output) + '\n'


def private_folder(path):
    path = path.absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise RepairError('Private authorization directory cannot use linked paths.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        literal = str(path).replace("'", "''")
        flags = subprocess.CREATE_NO_WINDOW
        identity = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                   '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                                  capture_output=True, text=True, timeout=15, creationflags=flags)
        sid = identity.stdout.strip()
        import re
        if identity.returncode or not re.fullmatch(r'S-1-(?:[0-9]+-)*[0-9]+', sid):
            raise RepairError('Unable to identify the user for private authorization storage.')
        # Elevated Windows shells commonly create directories owned by the
        # Administrators group. Explicitly set this one private directory's
        # owner to the user SID before verifying its protected ACL.
        owner = subprocess.run(['icacls.exe', str(path), '/setowner', '*' + sid],
                               capture_output=True, timeout=15, creationflags=flags)
        if owner.returncode:
            raise RepairError('Unable to set private authorization directory owner; no credentials saved.')
        # icacls changes the DACL only; Set-Acl can require audit privileges.
        reply = subprocess.run(['icacls.exe', str(path), '/grant:r',
                                '*' + sid + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F',
                                '*S-1-5-32-544:(OI)(CI)F', '/inheritance:r'],
                               capture_output=True, timeout=15, creationflags=flags)
        script = """$ErrorActionPreference='Stop'; $p='PATH';
$sids=@([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18','S-1-5-32-544');
$acl=Get-Acl -LiteralPath $p; $allowed=$sids+@('S-1-3-4');
$ok=$acl.AreAccessRulesProtected -and $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -eq $sids[0]; $found=@();
foreach($r in $acl.Access) {
  $sid=$r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;
  if($sid -notin $allowed -or $r.AccessControlType -ne 'Allow' -or $r.FileSystemRights -ne 'FullControl') {$ok=$false};
  $found+=$sid
};
foreach($sid in $sids) {if($sid -notin $found) {$ok=$false}};
if($ok) {'protected'} else {exit 1}
""".replace('PATH', literal)
        verified = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                  capture_output=True, text=True, timeout=15, creationflags=flags)
        if reply.returncode or verified.returncode or verified.stdout.strip() != 'protected':
            raise RepairError('Unable to protect authorization directory; no credentials saved.')
    else:
        if path.stat().st_uid != os.geteuid():
            raise RepairError('Private authorization directory must belong to the current user.')
        path.chmod(0o700)
    return path


def account_for(credentials):
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=credentials, cache_discovery=False).about().get(
        fields='user(emailAddress)').execute()['user']['emailAddress']


def authorize(args):
    from google_auth_oauthlib.flow import InstalledAppFlow
    env = read_env(args.root / '.env')
    if not all(env.get(key) for key in KEYS[:2]):
        raise RepairError('Existing OAuth client ID and secret are required in the source .env.')
    flow = InstalledAppFlow.from_client_config({'installed': {
        'client_id': env[KEYS[0]], 'client_secret': env[KEYS[1]],
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token',
        'redirect_uris': ['http://localhost'],
    }}, [SCOPE])
    print('Choose this Google account in the browser:', args.account)
    credentials = flow.run_local_server(host='127.0.0.1', port=0, timeout_seconds=300,
                                        prompt='consent select_account', login_hint=args.account,
                                        access_type='offline', authorization_prompt_message='Opening Google sign-in in your browser.',
                                        success_message='Sign-in completed. Return to the terminal.')
    email = account_for(credentials)
    if email.casefold() != args.account.casefold() or not credentials.refresh_token:
        raise RepairError('Selected account does not match, or no refresh token was returned. Nothing saved.')
    directory = private_folder(args.output.parent)
    if args.output.is_symlink() or args.output.exists():
        raise RepairError('Authorization output already exists; choose a new output filename.')
    bundle = {'account': email, KEYS[0]: env[KEYS[0]], KEYS[1]: env[KEYS[1]],
              KEYS[2]: credentials.refresh_token, 'scope': SCOPE}
    fd = os.open(directory / args.output.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(bundle, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    print('Verified Google account:', email)
    print('Private authorization saved:', args.output)
    print('Existing configurations and document IDs are unchanged. Do not paste the private file into chat.')


def apply(args):
    if os.name != 'posix':
        raise RepairError('Apply mode is intended for the Linux Contabo server.')
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    bundle = json.loads(args.credentials.read_text(encoding='utf-8'))
    replace_auth('', bundle)  # Validate before any file changes.
    credentials = Credentials(token=None, refresh_token=bundle[KEYS[2]],
                              token_uri='https://oauth2.googleapis.com/token',
                              client_id=bundle[KEYS[0]], client_secret=bundle[KEYS[1]])
    credentials.refresh(Request())
    email = account_for(credentials)
    if email.casefold() != args.account.casefold() or bundle.get('account', '').casefold() != email.casefold():
        raise RepairError('Authorization belongs to a different Google account. No configuration changed.')
    paths = [args.root / '.env', args.root / 'tm_manager' / '.env']
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise RepairError('Both application .env files must exist and must not be symlinks.')
    backups = private_folder(args.credentials.parent / ('backups-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')))
    originals = [(path, path.read_text(encoding='utf-8'), path.stat()) for path in paths]
    for index, (_, original, _) in enumerate(originals):
        backup = backups / ('scraper.env' if index == 0 else 'user-backend.env')
        backup.write_text(original, encoding='utf-8')
        backup.chmod(0o600)
    for path, original, stat in originals:
        fd, name = tempfile.mkstemp(prefix='.drive-repair-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(replace_auth(original, bundle))
                handle.flush()
                os.fsync(handle.fileno())
            os.chown(name, stat.st_uid, stat.st_gid)
            os.chmod(name, 0o600)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    print('Updated both application configurations for:', email)
    print('Private backups:', backups)
    print('Existing Drive folder IDs and all database/document records are unchanged.')
    print('Restart the application services, then verify the previously failing document before cleanup.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    local = modes.add_parser('authorize')
    local.add_argument('--root', type=Path, required=True)
    local.add_argument('--account', required=True)
    local.add_argument('--output', type=Path, required=True)
    server = modes.add_parser('apply')
    server.add_argument('--root', type=Path, required=True)
    server.add_argument('--account', required=True)
    server.add_argument('--credentials', type=Path, required=True)
    args = parser.parse_args()
    try:
        (authorize if args.mode == 'authorize' else apply)(args)
    except Exception as exc:
        # OAuth exceptions can contain token responses. Never print them.
        print('Repair stopped (' + type(exc).__name__ + '). No document files were uploaded or deleted.')
        if isinstance(exc, RepairError):
            print(str(exc))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
