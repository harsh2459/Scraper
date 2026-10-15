"""Self-enrollment: a machine with no credential of its own introduces itself with an enrollment key.

The coordinator answers with a credential for this machine alone and lists the machine as *waiting for
approval*. The credential is kept in the worker's storage folder, so later starts reuse it and no
secret has to be copied between machines. WORKER_TOKEN, when set, always wins (existing machines).
"""
import json
import os
import socket
import tempfile
import time
from pathlib import Path

import requests

IDENTITY_FILE = 'worker_identity.json'


class EnrollmentError(RuntimeError):
    pass


def load_identity(storage_dir, coordinator_url):
    path = Path(storage_dir) / IDENTITY_FILE
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    # A credential is only valid for the coordinator that issued it.
    if saved.get('coordinator_url') != coordinator_url or not isinstance(saved.get('token'), str) or len(saved['token']) < 24:
        return None
    return saved


def save_identity(storage_dir, identity):
    directory = Path(storage_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, delete=False) as handle:
        temp = Path(handle.name)
        json.dump(identity, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp, directory / IDENTITY_FILE)
        try:
            os.chmod(directory / IDENTITY_FILE, 0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def enroll(coordinator_url, enroll_key, storage_dir, hostname=None, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            with requests.Session() as session:
                session.trust_env = False
                reply = session.post(coordinator_url.rstrip('/') + '/api/control/v1/enroll', timeout=(5, 30), allow_redirects=False,
                                     json={'key': enroll_key, 'hostname': hostname or socket.gethostname()})
        except requests.RequestException as exc:
            last = EnrollmentError(f'Could not reach the coordinator to enroll this machine ({type(exc).__name__}).')
            time.sleep(2 * (attempt + 1))
            continue
        if reply.status_code == 201:
            body = reply.json()
            identity = {'coordinator_url': coordinator_url, 'worker_id': body['worker_id'], 'token': body['token'],
                        'enrolled_at': time.time()}
            save_identity(storage_dir, identity)
            return identity
        if reply.status_code == 403:
            raise EnrollmentError('The enrollment key was refused. It may be wrong, expired, revoked or already used by as many machines as it allows.')
        if reply.status_code == 429:
            raise EnrollmentError('Too many enrollment attempts. Wait a few minutes and try again.')
        last = EnrollmentError(f'The coordinator could not enroll this machine (HTTP {reply.status_code}).')
        time.sleep(2 * (attempt + 1))
    raise last


def resolve_token(environ, coordinator_url, storage_dir):
    """The credential this machine should use, enrolling it first if it has none. '' means there is none."""
    explicit = environ.get('WORKER_TOKEN', '')
    if explicit:
        return explicit
    saved = load_identity(storage_dir, coordinator_url)
    if saved:
        return saved['token']
    key = environ.get('WORKER_ENROLL_KEY', '')
    if not key:
        return ''
    return enroll(coordinator_url, key, storage_dir, environ.get('WORKER_MACHINE_NAME') or None)['token']
