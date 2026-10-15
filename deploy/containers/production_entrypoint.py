"""Explicit production runtime; independent from the staging DB guard."""
import os
import sys
from urllib.parse import urlparse


def validate(operation, env):
    if operation not in ('control', 'user', 'drive-preflight'):
        raise ValueError('Invalid production operation.')
    key = 'TM_DATABASE_URL' if operation == 'user' else 'CONTROL_DATABASE_URL'
    url = urlparse(env.get(key, ''))
    if url.scheme not in ('postgresql','postgres') or url.hostname != '127.0.0.1' or url.path != '/datascraper':
        raise ValueError('Production requires the audited loopback datascraper database.')
    if url.username != ('tm_live_user' if operation == 'user' else 'tm_live_control'):
        raise ValueError('Production requires its dedicated runtime role.')
    if operation == 'user' and (env.get('TM_AUTO_MIGRATE') != '0' or env.get('TM_BIND_HOST') != '127.0.0.1' or env.get('COOKIE_SECURE') != '1' or not env.get('SECRET_KEY')):
        raise ValueError('Invalid production user API configuration.')
    if operation != 'user' and env.get('CONTROL_STORAGE_MODE') not in ('local','drive'):
        raise ValueError('Production requires an explicit artifact storage mode.')
    if operation == 'drive-preflight' and env.get('CONTROL_STORAGE_MODE') != 'drive':
        raise ValueError('Drive verification requires Drive storage mode.')


if __name__ == '__main__':
    operation = sys.argv[1] if len(sys.argv) == 2 else ''
    validate(operation, os.environ)
    if os.geteuid() == 0:
        raise ValueError('Run production as a non-root application user.')
    os.chdir('/opt/tmtracker')
    commands = {'control': ['-m','control_plane'], 'user': ['/opt/tmtracker/tm_manager/app.py'],
                'drive-preflight': ['/usr/local/bin/tmtracker-drive-bootstrap.py','check']}
    os.execv(sys.executable, [sys.executable, *commands[operation]])
