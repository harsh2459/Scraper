"""Staging image guard: refuses production databases and root execution."""
import os
import sys
from urllib.parse import urlparse


def validate(operation, env):
    if operation not in ('migrate', 'control', 'user'):
        raise ValueError('Choose migrate, control or user.')
    key = 'TM_DATABASE_URL' if operation == 'user' else 'CONTROL_DATABASE_URL'
    database = urlparse(env.get(key, ''))
    if database.scheme not in ('postgresql', 'postgres') or database.hostname != '127.0.0.1':
        raise ValueError('Staging requires an explicit loopback PostgreSQL URL.')
    if not database.path.startswith('/fleet_staging_'):
        raise ValueError('Staging refuses databases without the fleet_staging_ prefix.')
    if operation == 'user' and (env.get('TM_AUTO_MIGRATE') != '0' or
                                env.get('TM_BACKGROUND_TASKS_ENABLED') != '0' or
                                env.get('TM_BIND_HOST') != '127.0.0.1' or
                                not env.get('SECRET_KEY')):
        raise ValueError('Staging user API requires disabled automatic migrations/background work, loopback binding and an explicit session secret.')


if __name__ == '__main__':
    operation = sys.argv[1] if len(sys.argv) == 2 else ''
    validate(operation, os.environ)
    if os.geteuid() == 0:
        raise ValueError('Run as the configured non-root application user.')
    os.chdir('/opt/tmtracker')
    commands = {'migrate': ['-m', 'migrations'], 'control': ['-m', 'control_plane'],
                'user': ['/opt/tmtracker/tm_manager/app.py']}
    os.execv(sys.executable, [sys.executable, *commands[operation]])
