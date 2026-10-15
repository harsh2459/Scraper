import hmac
from pathlib import Path
import threading
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

from .config import WorkerSettings
from .engine import WorkerEngine


def create_app(settings=None, engine=None):
    settings = settings or WorkerSettings.from_env()
    engine = engine or WorkerEngine(settings)
    app = Flask(__name__)
    app.extensions['engine'] = engine

    @app.before_request
    def authorize():
        if not request.path.startswith('/api/worker/'):
            return None
        if request.path == '/api/worker/health':
            return None
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:
            return jsonify(error='Cross-origin controls are disabled.'), 403
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        if not token or not hmac.compare_digest(token, settings.local_token):
            return jsonify(error='Local worker authorization required.'), 401

    @app.get('/api/worker/health')
    def health():
        return jsonify(status='ok')

    @app.get('/api/worker/status')
    def status():
        return jsonify(engine.status())

    @app.get('/api/worker/assignments')
    def assignments():
        return jsonify(items=engine.store.list(), counts=engine.store.counts())

    @app.get('/api/worker/events')
    def events():
        return jsonify(items=engine.store.events())

    @app.post('/api/worker/pause')
    def pause():
        engine.store.set('paused', True)
        return jsonify(ok=True)

    @app.post('/api/worker/resume')
    def resume():
        engine.store.set('paused', False)
        return jsonify(ok=True)

    @app.post('/api/worker/cycle')
    def cycle():
        if engine.lock.locked():
            return jsonify(error='A cycle is already running.'), 409
        threading.Thread(target=engine.cycle, daemon=True).start()
        return jsonify(ok=True), 202

    @app.post('/api/worker/reextract')
    def reextract():
        if not engine.lock.acquire(blocking=False):
            return jsonify(error='A cycle is already running.'), 409
        try:
            result_id = (request.get_json() or {}).get('result_id')
            engine.store.result_by_id(result_id)
            engine.vpn.disconnect()
            response = engine.client.repair(result_id)
        except Exception:
            return jsonify(error='Cannot request repair. Check coordinator connectivity, local files and pending refreshes.'), 409
        finally:
            engine.lock.release()
        threading.Thread(target=engine.cycle, daemon=True).start()
        return jsonify(response), 202

    @app.get('/')
    @app.get('/<path:path>')
    def frontend(path='index.html'):
        root = Path(__file__).parent.parent / 'worker_frontend' / 'dist'
        return send_from_directory(root, path if (root / path).is_file() else 'index.html')

    return app
