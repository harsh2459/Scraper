import logging
import os
import threading
from waitress import serve
from .app import create_app
from .config import WorkerSettings

if __name__ == '__main__':
    # INFO shows login/OTP/IMAP progress in worker-agent.err; fake_useragent warns on every request.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    logging.getLogger('fake_useragent').setLevel(logging.ERROR)
    logging.getLogger('waitress').setLevel(logging.WARNING)
    settings = WorkerSettings.from_env()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    # Prevent two agent processes using one outbox/VPN simultaneously.
    lockfile = (settings.storage_dir / 'agent.lock').open('a+b')
    lockfile.seek(0)
    lockfile.write(b'0')
    lockfile.flush()
    lockfile.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    app = create_app(settings)
    stop = threading.Event()
    threading.Thread(target=app.extensions['engine'].background, args=(stop,), daemon=True).start()
    serve(app, host='127.0.0.1', port=settings.port, threads=4)
