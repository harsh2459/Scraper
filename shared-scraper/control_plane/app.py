import datetime
import io
import hmac
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time

import psycopg
from flask import Flask, g, jsonify, request, send_file, send_from_directory
from flask.json.provider import DefaultJSONProvider

from .config import Settings
from .contracts import digest, identifier, timestamp
from .repository import Conflict, Repository
from fleet_storage import DriveArchive, StorageUnavailable, archive_upload, artifact_bytes


class ISOJSON(DefaultJSONProvider):
    @staticmethod
    def default(value):
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        return DefaultJSONProvider.default(value)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.json_provider_class = ISOJSON
    app.json = ISOJSON(app)
    app.config['MAX_CONTENT_LENGTH'] = settings.max_upload_bytes
    repo = Repository(settings)
    app.extensions['repository'] = repo
    enroll_failures = {}

    @app.before_request
    def authenticate():
        if request.path in ('/api/control/v1/health', '/api/control/v1/enroll') or not request.path.startswith('/api/control/'):
            return None
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        if request.path.startswith('/api/control/v1/admin/'):
            if not token or not hmac.compare_digest(token, settings.admin_token):
                return jsonify(error='Admin authorization required.'), 401
            g.actor = 'admin'
        else:
            worker = repo.authenticate_worker(token) if token else None
            if not worker:
                return jsonify(error='Worker authorization required.'), 401
            g.worker = worker

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 409 if isinstance(exc, Conflict) else 400

    @app.errorhandler(StorageUnavailable)
    def storage_error(exc):
        return jsonify(error=str(exc)), 503

    @app.errorhandler(psycopg.errors.UniqueViolation)
    def duplicate(_exc):
        return jsonify(error='This record already exists.'), 409

    @app.errorhandler(psycopg.Error)
    def database_error(_exc):
        logging.exception('Control database operation failed')
        return jsonify(error='Database operation failed; retry later.'), 503

    @app.get('/api/control/v1/health')
    def health():
        return jsonify(status='ok', schema_version=1)

    @app.get('/api/control/v1/admin/overview')
    def overview():
        return jsonify(repo.overview())

    @app.get('/api/control/v1/admin/<collection>')
    def collection(collection):
        limit = min(100, max(1, request.args.get('limit', 50, type=int)))
        offset = max(0, request.args.get('offset', 0, type=int))
        return jsonify(repo.list(collection, limit, offset))

    @app.post('/api/control/v1/admin/workers')
    def add_worker():
        body = request.get_json() or {}
        return jsonify(repo.create_worker(body.get('name'), body.get('capacity', 5))), 201

    @app.post('/api/control/v1/enroll')
    def enroll():
        # No credential yet: the enrollment key IS the credential. Failures are rate limited per client.
        client = (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()) or 'unknown'
        now = time.monotonic()
        failures = [t for t in enroll_failures.get(client, []) if now - t < 600]
        if len(failures) >= 20:
            return jsonify(error='Too many attempts. Try again in a few minutes.'), 429
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(repo.enroll_worker(body.get('key'), body.get('hostname'))), 201
        except PermissionError as exc:
            enroll_failures[client] = failures + [now]
            return jsonify(error=str(exc)), 403

    @app.get('/api/control/v1/admin/enrollment-keys')
    def enrollment_keys():
        return jsonify(items=repo.list_enrollment_keys())

    @app.post('/api/control/v1/admin/enrollment-keys')
    def create_enrollment_key():
        body = request.get_json() or {}
        return jsonify(repo.create_enrollment_key(body.get('days'), body.get('max_uses'), body.get('label'))), 201

    @app.post('/api/control/v1/admin/enrollment-keys/<key_id>/revoke')
    def revoke_enrollment_key(key_id):
        repo.revoke_enrollment_key(key_id)
        return jsonify(ok=True)

    @app.post('/api/control/v1/admin/workers/<worker_id>/approve')
    def approve_worker(worker_id):
        body = request.get_json() or {}
        return jsonify(repo.approve_worker(worker_id, body.get('name'), body.get('capacity', 5)))

    @app.get('/api/control/v1/admin/workers/<worker_id>/summary')
    def worker_summary(worker_id):
        return jsonify(repo.worker_summary(worker_id))

    @app.patch('/api/control/v1/admin/workers/<worker_id>')
    def edit_worker(worker_id):
        repo.update_worker(identifier(worker_id), request.get_json() or {})
        return jsonify(ok=True)

    @app.post('/api/control/v1/admin/workers/<worker_id>/rotate-token')
    def rotate(worker_id):
        return jsonify(repo.rotate_token(identifier(worker_id)))

    @app.post('/api/control/v1/admin/jobs')
    def add_job():
        body = request.get_json() or {}
        return jsonify(job_id=repo.enqueue(body.get('app_no'), body.get('priority', 100)))

    @app.get('/api/control/v1/admin/queue-overview')
    def queue_overview():
        return jsonify(repo.queue_overview(request.args.get('view', 'attention'), request.args.get('limit', 50, type=int), request.args.get('offset', 0, type=int)))

    @app.post('/api/control/v1/admin/queue')
    def queue_trademarks():
        body = request.get_json() or {}
        return jsonify(repo.queue_trademarks(body.get('app_nos'), body.get('priority', 'normal')))

    @app.post('/api/control/v1/admin/dispatch')
    def dispatch():
        return jsonify(repo.dispatch())

    @app.patch('/api/control/v1/admin/jobs/<job_id>')
    def edit_job(job_id):
        return jsonify(repo.edit_job(identifier(job_id), request.get_json() or {}))

    @app.get('/api/control/v1/admin/status-schedule')
    def status_schedule():
        return jsonify(repo.status_schedule())

    @app.post('/api/control/v1/admin/status-schedule')
    def set_status_schedule():
        body = request.get_json() or {}
        return jsonify(repo.set_status_interval(body.get('status'), body.get('interval_days')))

    @app.post('/api/control/v1/admin/status-schedule/defaults')
    def status_schedule_defaults():
        return jsonify(repo.load_status_defaults())

    # POST is accepted as well as PUT: the user backend's admin proxy does not forward PUT.
    @app.route('/api/control/v1/admin/schedules/<app_no>', methods=['PUT', 'POST'])
    def schedule(app_no):
        body = request.get_json() or {}
        date = timestamp(body['next_fetch_at']) if body.get('next_fetch_at') else None
        repo.schedule(app_no, body.get('policy_id'), date)
        return jsonify(ok=True)

    @app.post('/api/control/v1/admin/policies')
    def policy():
        body = request.get_json() or {}
        repo.create_policy(body.get('id'), body.get('name'), body.get('interval_days'), body.get('match_status'))
        return jsonify(ok=True)

    @app.post('/api/control/v1/admin/accounts')
    def add_account():
        return jsonify(id=repo.add_account(request.get_json() or {})), 201

    @app.get('/api/control/v1/admin/accounts-overview')
    def accounts_overview():
        return jsonify(repo.accounts_overview())

    @app.post('/api/control/v1/admin/accounts/assign')
    def assign_accounts():
        body = request.get_json() or {}
        # No worker_id key at all means: leave each account on its current machine.
        return jsonify(repo.assign_accounts(body.get('account_ids'), body.get('worker_id'), body.get('enabled'), keep_machine='worker_id' not in body))

    @app.patch('/api/control/v1/admin/accounts/<account_id>')
    def assign_account(account_id):
        repo.assign_account(identifier(account_id), request.get_json() or {})
        return jsonify(ok=True)

    @app.get('/api/control/v1/worker/configuration')
    def configuration():
        return jsonify(repo.configuration(g.worker['id']))

    @app.post('/api/control/v1/worker/heartbeat')
    def heartbeat():
        body = request.get_json() or {}
        repo.heartbeat(g.worker['id'], body.get('phase', 'idle'), body.get('metadata', {}))
        return jsonify(ok=True, worker_status=g.worker['status'])

    @app.post('/api/control/v1/worker/claim')
    def claim():
        repo.dispatch()
        body = request.get_json() or {}
        jobs = repo.claim(g.worker['id'], body.get('limit', 5), body.get('repair_only', False))
        return jsonify(schema_version=1, assignments=jobs)

    @app.post('/api/control/v1/worker/offline')
    def offline():
        repo.acknowledge_offline(g.worker['id'], (request.get_json() or {}).get('assignments'))
        return jsonify(ok=True)

    @app.get('/api/control/v1/worker/reconcile')
    def reconcile():
        repo.dispatch()
        return jsonify(repo.reconcile(g.worker['id']))

    @app.put('/api/control/v1/worker/artifacts/<result_id>/<sha>')
    def artifact(result_id, sha):
        if not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('Invalid checksum.')
        repo.authorize_upload(g.worker['id'], result_id, request.headers.get('X-Job-ID'), request.headers.get('X-Reservation-Token'))
        content = request.get_data()
        if not content or digest(content) != sha:
            raise ValueError('Artifact checksum does not match its bytes.')
        target = settings.artifact_dir / g.worker['id'] / identifier(result_id) / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, target)
            repo.record_artifact(g.worker['id'], result_id, sha, len(content))
            if settings.storage_mode == 'drive':
                try:
                    archive = DriveArchive(settings.drive_options)
                except Exception:
                    raise StorageUnavailable('Drive connection unavailable; local copy retained.') from None
                archive_upload(repo, g.worker['id'], result_id, sha, content, archive)
        finally:
            temp.unlink(missing_ok=True)
        return jsonify(ok=True, sha256=sha)

    @app.post('/api/control/v1/worker/complete')
    def complete():
        return jsonify(repo.complete(g.worker['id'], request.get_json() or {}))

    @app.post('/api/control/v1/worker/fail')
    def fail():
        return jsonify(repo.fail(g.worker['id'], request.get_json() or {}))

    @app.post('/api/control/v1/worker/repair')
    def extraction_repair():
        return jsonify(repo.request_extraction_repair(g.worker['id'], (request.get_json() or {}).get('result_id')))

    @app.get('/api/control/v1/admin/results/<result_id>')
    def result(result_id):
        with repo.transaction() as conn:
            row = conn.execute('SELECT * FROM results WHERE id=%s', (identifier(result_id),)).fetchone()
            return (jsonify(row), 200) if row else (jsonify(error='Result not found.'), 404)

    @app.get('/api/control/v1/admin/results/<result_id>/artifacts/<sha>')
    def download(result_id, sha):
        if not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('Invalid checksum.')
        with repo.transaction() as conn:
            row = conn.execute('SELECT worker_id,payload FROM results WHERE id=%s', (identifier(result_id),)).fetchone()
            artifact = next((a for a in row['payload']['artifacts'] if a['sha256'] == sha), None) if row else None
        if not artifact:
            return jsonify(error='Artifact not found.'), 404
        with repo.transaction() as conn:
            content = artifact_bytes(conn, {**artifact, 'worker_id': row['worker_id'], 'result_id': result_id}, settings.artifact_dir, settings.drive_options)
        return send_file(io.BytesIO(content),
                         as_attachment=True, download_name=artifact['filename'])

    @app.get('/admin/')
    @app.get('/admin/<path:path>')
    def frontend(path='index.html'):
        root = Path(__file__).parent.parent / 'admin_frontend' / 'dist'
        return send_from_directory(root, path if (root / path).is_file() else 'index.html')

    return app


def start_dispatcher(app, stop=None):
    stop = stop or threading.Event()
    try:  # Make sure every portal status has a refresh interval; existing (edited) rules are never changed.
        app.extensions['repository'].load_status_defaults()
    except Exception:
        logging.exception('Could not load default status schedule')
    def loop():
        while not stop.is_set():
            try:
                app.extensions['repository'].dispatch()
            except Exception:
                logging.exception('Dispatcher tick failed')
            stop.wait(15)
    thread = threading.Thread(target=loop, daemon=True, name='control-dispatcher')
    thread.start()
    return stop
