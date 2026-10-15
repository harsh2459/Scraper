import datetime as dt
import hashlib
import json
import re
import secrets
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import application_number, fingerprint, identifier, utcnow, validate_result


class Conflict(ValueError):
    pass


def uid():
    return str(uuid.uuid4())


class Repository:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.settings.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute("SET LOCAL search_path TO fleet_control, public")
            yield conn

    def audit(self, conn, actor, action, details):
        conn.execute('INSERT INTO audit(actor,action,details) VALUES(%s,%s,%s)', (actor, action, Jsonb(details)))

    def authenticate_worker(self, token):
        with self.transaction() as conn:
            return conn.execute("SELECT * FROM workers WHERE token_hash=%s AND status <> 'retired'",
                                (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()

    def create_worker(self, name, capacity=5):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('Worker name is required (maximum 80 characters).')
        if type(capacity) is not int or not 1 <= capacity <= 100:
            raise ValueError('Capacity must be between 1 and 100.')
        token = secrets.token_urlsafe(32)
        worker_id = uid()
        with self.transaction() as conn:
            conn.execute('INSERT INTO workers(id,name,capacity,token_hash) VALUES(%s,%s,%s,%s)',
                         (worker_id, name.strip(), capacity, hashlib.sha256(token.encode()).hexdigest()))
            self.audit(conn, 'admin', 'worker.created', {'worker_id': worker_id, 'name': name})
        return {'id': worker_id, 'name': name, 'token': token}

    def update_worker(self, worker_id, body):
        status = body.get('status')
        capacity = body.get('capacity')
        if status not in ('active', 'paused', 'draining', 'retired'):
            raise ValueError('Invalid worker status.')
        if type(capacity) is not int or not 1 <= capacity <= 100:
            raise ValueError('Capacity must be between 1 and 100.')
        with self.transaction() as conn:
            current = conn.execute('SELECT status FROM workers WHERE id=%s FOR UPDATE', (worker_id,)).fetchone()
            if not current:
                raise ValueError('Worker not found.')
            if current['status'] == 'pending' and status != 'retired':
                raise ValueError('This machine is waiting for approval. Approve it first.')
            conn.execute('UPDATE workers SET status=%s,capacity=%s WHERE id=%s', (status, capacity, worker_id))
            self.audit(conn, 'admin', 'worker.updated', {'worker_id': worker_id, 'status': status, 'capacity': capacity})

    # --- Machines that introduce themselves ----------------------------------------------------
    @staticmethod
    def _key_hash(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def create_enrollment_key(self, days=None, max_uses=None, label=None):
        """By default a key never expires and enrolls any number of machines; an administrator revokes it by hand."""
        if days is not None and (type(days) is not int or not 1 <= days <= 3650):
            raise ValueError('A key can be valid for 1 to 3650 days, or have no end date.')
        if max_uses is not None and (type(max_uses) is not int or not 1 <= max_uses <= 1000):
            raise ValueError('Number of machines must be between 1 and 1000.')
        label = (label or '').strip()[:80] or None
        key = 'tmk_' + secrets.token_urlsafe(24)
        key_id = uid()
        expires = utcnow() + dt.timedelta(days=days) if days is not None else None
        with self.transaction() as conn:
            conn.execute('INSERT INTO enrollment_keys(id,key_hash,label,expires_at,max_uses) VALUES(%s,%s,%s,%s,%s)',
                         (key_id, self._key_hash(key), label, expires, max_uses))
            self.audit(conn, 'admin', 'enrollment_key.created', {'id': key_id, 'days': days, 'max_uses': max_uses})
        return {'id': key_id, 'key': key, 'expires_at': expires, 'max_uses': max_uses}

    def list_enrollment_keys(self):
        with self.transaction() as conn:
            return conn.execute('SELECT id,label,created_at,expires_at,revoked_at,max_uses,uses FROM enrollment_keys '
                                'ORDER BY created_at DESC LIMIT 50').fetchall()

    def revoke_enrollment_key(self, key_id):
        with self.transaction() as conn:
            row = conn.execute('UPDATE enrollment_keys SET revoked_at=COALESCE(revoked_at,now()) WHERE id=%s RETURNING id', (identifier(key_id),)).fetchone()
            if not row:
                raise ValueError('Key not found.')
            self.audit(conn, 'admin', 'enrollment_key.revoked', {'id': key_id})

    def enroll_worker(self, key, hostname):
        """A machine presents an enrollment key. It becomes a *pending* machine with its own credential."""
        if not isinstance(key, str) or not key:
            raise PermissionError('Invalid or expired enrollment key.')
        host = re.sub(r'[^A-Za-z0-9 ._-]', '', str(hostname or '')).strip()[:60] or 'new-machine'
        token = secrets.token_urlsafe(32)
        worker_id = uid()
        with self.transaction() as conn:
            row = conn.execute('SELECT id FROM enrollment_keys WHERE key_hash=%s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now()) '
                               'AND (max_uses IS NULL OR uses<max_uses) FOR UPDATE', (self._key_hash(key),)).fetchone()
            if not row:
                raise PermissionError('Invalid or expired enrollment key.')
            name, n = host, 1
            while conn.execute('SELECT 1 FROM workers WHERE lower(name)=lower(%s)', (name,)).fetchone():
                n += 1
                name = f'{host} ({n})'
            conn.execute("INSERT INTO workers(id,name,status,capacity,token_hash,enrolled_hostname) VALUES(%s,%s,'pending',5,%s,%s)",
                         (worker_id, name, hashlib.sha256(token.encode()).hexdigest(), host))
            conn.execute('UPDATE enrollment_keys SET uses=uses+1 WHERE id=%s', (row['id'],))
            self.audit(conn, 'enrollment', 'worker.enrolled', {'worker_id': worker_id, 'hostname': host, 'key_id': row['id']})
        return {'worker_id': worker_id, 'token': token}

    def approve_worker(self, worker_id, name, capacity=5):
        worker_id = identifier(worker_id)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError('Give the machine a name (up to 80 characters).')
        if type(capacity) is not int or not 1 <= capacity <= 100:
            raise ValueError('Trademarks per batch must be between 1 and 100.')
        name = name.strip()
        with self.transaction() as conn:
            worker = conn.execute('SELECT status FROM workers WHERE id=%s FOR UPDATE', (worker_id,)).fetchone()
            if not worker:
                raise ValueError('Machine not found.')
            if worker['status'] != 'pending':
                raise ValueError('This machine is already approved.')
            if conn.execute('SELECT 1 FROM workers WHERE lower(name)=lower(%s) AND id<>%s', (name, worker_id)).fetchone():
                raise ValueError('Another machine already has that name. Choose a different one.')
            conn.execute("UPDATE workers SET name=%s,capacity=%s,status='active' WHERE id=%s", (name, capacity, worker_id))
            self.audit(conn, 'admin', 'worker.approved', {'worker_id': worker_id, 'name': name})
        return {'id': worker_id, 'name': name}

    def worker_summary(self, worker_id):
        """Everything the 'Manage machine' panel shows, in plain terms (no credentials, no raw rows)."""
        worker_id = identifier(worker_id)

        def mask(email):
            user, _, domain = (email or '').partition('@')
            return (user[:3] + '***@' + domain) if domain else '***'

        with self.transaction() as conn:
            worker = conn.execute('SELECT id,name,status,capacity,phase,last_contact_at,offline_until,created_at,metadata '
                                  'FROM workers WHERE id=%s', (worker_id,)).fetchone()
            if not worker:
                raise ValueError('Machine not found.')
            accounts = [{'id': a['id'], 'email': mask(a['email']), 'enabled': a['enabled']} for a in conn.execute(
                'SELECT id,email,enabled FROM accounts WHERE worker_id=%s ORDER BY created_at', (worker_id,)).fetchall()]
            jobs = {r['state']: r['n'] for r in conn.execute(
                'SELECT state,count(*) AS n FROM jobs WHERE worker_id=%s GROUP BY state', (worker_id,)).fetchall()}
            results = conn.execute('SELECT app_no,disposition,received_at FROM results WHERE worker_id=%s '
                                   'ORDER BY received_at DESC LIMIT 8', (worker_id,)).fetchall()
            totals = {r['disposition']: r['n'] for r in conn.execute(
                'SELECT disposition,count(*) AS n FROM results WHERE worker_id=%s GROUP BY disposition', (worker_id,)).fetchall()}
            problems = conn.execute("SELECT app_no,state,last_error,attempts,reserved_at FROM jobs WHERE worker_id=%s "
                                    "AND last_error IS NOT NULL AND last_error<>'' ORDER BY reserved_at DESC NULLS LAST LIMIT 5",
                                    (worker_id,)).fetchall()
        return {'worker': worker, 'accounts': accounts, 'jobs': jobs, 'recent_results': results,
                'result_totals': totals, 'recent_problems': problems}

    def rotate_token(self, worker_id):
        token = secrets.token_urlsafe(32)
        with self.transaction() as conn:
            row = conn.execute('UPDATE workers SET token_hash=%s WHERE id=%s RETURNING id',
                               (hashlib.sha256(token.encode()).hexdigest(), worker_id)).fetchone()
            if not row:
                raise ValueError('Worker not found.')
            self.audit(conn, 'admin', 'worker.token_rotated', {'worker_id': worker_id})
        return {'token': token}

    def heartbeat(self, worker_id, phase, metadata):
        if phase not in ('idle', 'reserved', 'scraping', 'upload_pending', 'syncing', 'paused', 'error'):
            raise ValueError('Invalid phase.')
        if not isinstance(metadata, dict):
            raise ValueError('Metadata must be an object.')
        with self.transaction() as conn:
            conn.execute('UPDATE workers SET last_contact_at=now(),phase=%s,metadata=%s WHERE id=%s',
                         (phase, Jsonb(metadata), worker_id))

    def _enqueue(self, conn, app_no, priority, reason, force=False):
        application_number(app_no)
        conn.execute('INSERT INTO trademarks(app_no,policy_id,next_fetch_at) VALUES(%s,%s,now()) ON CONFLICT DO NOTHING',
                     (app_no, 'four_weeks'))
        tm = conn.execute('SELECT * FROM trademarks WHERE app_no=%s FOR UPDATE', (app_no,)).fetchone()
        existing = conn.execute("SELECT * FROM jobs WHERE app_no=%s AND state IN ('queued','reserved','retry_wait')",
                                (app_no,)).fetchone()
        if existing:
            conn.execute('UPDATE jobs SET priority=GREATEST(priority,%s) WHERE id=%s', (priority, existing['id']))
            return existing['id']
        if not force and tm['last_successful_fetch_at'] and (tm['next_fetch_at'] is None or tm['next_fetch_at'] > utcnow()):
            if self.settings.publish_legacy:
                from .legacy_projection import attach_cached
                attach_cached(conn, app_no)
            return None
        generation = tm['generation'] + 1
        job_id = uid()
        conn.execute('UPDATE trademarks SET generation=%s WHERE app_no=%s', (generation, app_no))
        conn.execute('INSERT INTO jobs(id,app_no,generation,priority,reason,due_at) VALUES(%s,%s,%s,%s,%s,%s)',
                     (job_id, app_no, generation, priority, reason, tm['next_fetch_at'] or utcnow()))
        return job_id

    def enqueue(self, app_no, priority=100, reason='manual'):
        if type(priority) is not int or not 0 <= priority <= 1000:
            raise ValueError('Priority must be between 0 and 1000.')
        with self.transaction() as conn:
            job = self._enqueue(conn, app_no, priority, reason, force=True)
            self.audit(conn, 'admin', 'job.requested', {'app_no': app_no, 'job_id': job})
            return job

    def _expire_reservations(self):
        # Commit expiry before scheduling: scheduling locks application rows,
        # while expiry only locks jobs. Keeping these separate avoids an
        # inversion with result acceptance under concurrent dispatch.
        with self.transaction() as conn:
            expired = conn.execute("SELECT * FROM jobs WHERE state='reserved' AND expires_at < now() "
                                   "- make_interval(secs=>%s) FOR UPDATE SKIP LOCKED", (self.settings.grace_seconds,)).fetchall()
            for job in expired:
                conn.execute("UPDATE attempts SET finished_at=now(),outcome='expired' WHERE reservation_token=%s AND finished_at IS NULL",
                             (job['reservation_token'],))
                state = 'failed' if job['attempts'] >= self.settings.max_attempts else 'queued'
                conn.execute('UPDATE jobs SET state=%s,available_at=now(),last_error=%s WHERE id=%s',
                             (state, 'Offline reservation expired', job['id']))
            return len(expired)

    def dispatch(self, limit=100):
        expired_count = self._expire_reservations()
        with self.transaction() as conn:
            # Serialize scheduling passes without blocking other coordinator replicas.
            if not conn.execute('SELECT pg_try_advisory_xact_lock(84101733) AS locked').fetchone()['locked']:
                return {'requests': 0, 'scheduled': 0, 'expired': 0}
            requests = conn.execute('SELECT * FROM public.fleet_fetch_requests WHERE consumed_at IS NULL '
                                    'ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED', (limit,)).fetchall()
            for request in requests:
                self._enqueue(conn, request['app_no'], request['priority'], request['reason'],
                              force=request['reason'] != 'initial')
                conn.execute('UPDATE public.fleet_fetch_requests SET consumed_at=now() WHERE id=%s', (request['id'],))
            # Owner-aware: applications whose every owner is deactivated are not queued, and
            # scheduled work carries the best priority among its active owners (0 if untracked).
            # tm_owner_priority exposes only app_no/priority/has_active_owner, never user data.
            due = conn.execute("SELECT t.app_no, LEAST(1000, GREATEST(0, COALESCE(o.priority, 0))) AS priority "
                               "FROM trademarks t LEFT JOIN tm_owner_priority o ON o.app_no=t.app_no "
                               "WHERE t.next_fetch_at <= now() AND (o.app_no IS NULL OR o.has_active_owner) "
                               "AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.app_no=t.app_no "
                               "AND (j.state IN ('queued','reserved','retry_wait') OR (j.state IN ('failed','cancelled') AND j.generation=t.generation))) "
                               'ORDER BY COALESCE(o.priority, 0) DESC, t.next_fetch_at LIMIT %s FOR UPDATE OF t SKIP LOCKED', (limit,)).fetchall()
            for row in due:
                self._enqueue(conn, row['app_no'], row['priority'], 'scheduled')
            return {'requests': len(requests), 'scheduled': len(due), 'expired': expired_count}

    def claim(self, worker_id, limit, repair_only=False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Batch limit must be between 1 and 100.')
        if type(repair_only) is not bool:
            raise ValueError('repair_only must be a boolean.')
        with self.transaction() as conn:
            worker = conn.execute('SELECT * FROM workers WHERE id=%s FOR UPDATE', (worker_id,)).fetchone()
            if worker['status'] != 'active':
                return []
            # Outstanding reservations are returned on retry, even after midnight.
            jobs = conn.execute("SELECT * FROM jobs WHERE worker_id=%s AND state='reserved' ORDER BY due_at", (worker_id,)).fetchall()
            if jobs:
                return [job for job in jobs if not repair_only or job['repair_result_id']]
            jobs = conn.execute("SELECT * FROM jobs WHERE state IN ('queued','retry_wait') AND available_at<=now() "
                                'AND (target_worker_id IS NULL OR target_worker_id=%s) '
                                'AND (NOT %s OR repair_result_id IS NOT NULL) '
                                'ORDER BY priority DESC,due_at,id LIMIT %s FOR UPDATE SKIP LOCKED',
                                (worker_id, repair_only, min(limit, worker['capacity']))).fetchall()
            if not jobs:
                conn.execute("UPDATE workers SET last_contact_at=now(),phase='idle',offline_until=NULL WHERE id=%s", (worker_id,))
                return []
            now = utcnow()
            expires = now + dt.timedelta(seconds=self.settings.reservation_seconds)
            for job in jobs:
                token = uid()
                conn.execute("UPDATE jobs SET state='reserved',worker_id=%s,reservation_token=%s,reserved_at=%s,"
                             'expires_at=%s,attempts=attempts+1 WHERE id=%s', (worker_id, token, now, expires, job['id']))
                conn.execute('INSERT INTO attempts(id,job_id,worker_id,reservation_token,started_at) VALUES(%s,%s,%s,%s,%s)',
                             (uid(), job['id'], worker_id, token, now))
            conn.execute("UPDATE workers SET last_contact_at=now(),phase='reserved',offline_until=%s WHERE id=%s", (expires, worker_id))
            return conn.execute("SELECT * FROM jobs WHERE worker_id=%s AND state='reserved' ORDER BY due_at", (worker_id,)).fetchall()

    def acknowledge_offline(self, worker_id, assignments):
        if not isinstance(assignments, list) or not assignments:
            raise ValueError('Assignments are required.')
        with self.transaction() as conn:
            for item in assignments:
                row = conn.execute("SELECT id FROM jobs WHERE id=%s AND worker_id=%s AND reservation_token=%s "
                                   "AND state='reserved' AND expires_at>now() FOR UPDATE",
                                   (identifier(item.get('id')), worker_id, identifier(item.get('reservation_token')))).fetchone()
                if not row:
                    raise Conflict('Assignment is no longer valid; reconcile before connecting VPN.')
            conn.execute("UPDATE workers SET phase='scraping',last_contact_at=now() WHERE id=%s", (worker_id,))

    def authorize_upload(self, worker_id, result_id, job_id, token):
        identifier(result_id)
        with self.transaction() as conn:
            row = conn.execute('SELECT id FROM attempts WHERE worker_id=%s AND job_id=%s AND reservation_token=%s',
                               (worker_id, identifier(job_id), identifier(token))).fetchone()
            if not row:
                raise Conflict('Upload does not belong to an assignment held by this worker.')

    def record_artifact(self, worker_id, result_id, sha, size):
        with self.transaction() as conn:
            conn.execute('INSERT INTO artifacts(worker_id,result_id,sha256,size) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (worker_id, result_id, sha, size))

    def _retry(self, conn, job, message, permanent=False):
        failed = permanent or job['attempts'] >= self.settings.max_attempts
        available = utcnow() + dt.timedelta(seconds=min(3600, 30 * 2 ** min(job['attempts'], 6)))
        conn.execute('UPDATE jobs SET state=%s,available_at=%s,last_error=%s WHERE id=%s',
                     ('failed' if failed else 'retry_wait', available, message[:1000], job['id']))

    def fail(self, worker_id, body):
        with self.transaction() as conn:
            job = conn.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (identifier(body.get('job_id')),)).fetchone()
            if not job or job['worker_id'] != worker_id or job['reservation_token'] != body.get('reservation_token') or job['state'] != 'reserved':
                return {'disposition': 'obsolete'}
            message = str(body.get('error', 'Scrape failed'))[:1000]
            self._retry(conn, job, message, body.get('retryable') is False)
            conn.execute("UPDATE attempts SET finished_at=now(),outcome='failed',error=%s WHERE reservation_token=%s", (message, job['reservation_token']))
            return {'disposition': 'recorded'}

    def complete(self, worker_id, body):
        from .contracts import timestamp
        observed = validate_result(body)
        result_id = body['result_id']
        with self.transaction() as conn:
            job = conn.execute('SELECT * FROM jobs WHERE id=%s', (body['job_id'],)).fetchone()
            if not job or job['app_no'] != body['app_no'] or job['generation'] != body['generation']:
                raise Conflict('Result does not match its job.')
            # Same application -> job lock order as enqueue/scheduling.
            tm = conn.execute('SELECT * FROM trademarks WHERE app_no=%s FOR UPDATE', (body['app_no'],)).fetchone()
            job = conn.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (body['job_id'],)).fetchone()
            old = conn.execute('SELECT * FROM results WHERE id=%s', (result_id,)).fetchone()
            if old:
                if old['worker_id'] != worker_id or old['fingerprint'] != fingerprint(body):
                    raise Conflict('Result identity was reused with different contents.')
                return {'result_id': result_id, 'disposition': old['disposition'], 'acknowledged': True}
            attempt = conn.execute('SELECT * FROM attempts WHERE worker_id=%s AND job_id=%s AND reservation_token=%s',
                                   (worker_id, job['id'], body['reservation_token'])).fetchone()
            if not attempt or observed < attempt['started_at'] - dt.timedelta(minutes=5):
                raise Conflict('Result is not from a valid worker reservation.')
            for artifact in body.get('artifacts', []):
                saved = conn.execute('SELECT size FROM artifacts WHERE worker_id=%s AND result_id=%s AND sha256=%s',
                                     (worker_id, result_id, artifact['sha256'])).fetchone()
                if not saved or saved['size'] != artifact['size']:
                    raise Conflict('Upload every artifact before completing the result.')
                if self.settings.storage_mode == 'drive':
                    remote = conn.execute('SELECT verified_at FROM artifact_storage WHERE worker_id=%s AND result_id=%s AND sha256=%s',
                                          (worker_id, result_id, artifact['sha256'])).fetchone()
                    if not remote or not remote['verified_at']:
                        raise Conflict('Verify the Drive copy before completing the result.')
                path = self.settings.artifact_dir / worker_id / result_id / artifact['sha256']
                if not path.is_file() or path.stat().st_size != artifact['size']:
                    raise Conflict('Artifact storage is unavailable; retry its upload.')
            current = (job['state'] == 'reserved' and job['worker_id'] == worker_id
                       and job['reservation_token'] == body['reservation_token'] and tm['generation'] == job['generation'])
            if bool(job['repair_result_id']) != bool(body.get('repair_only')):
                raise Conflict('Extraction repair does not match the assignment.')
            if not job['repair_result_id'] and body.get('portal_observed_at'):
                raise Conflict('Normal refreshes must use their new observation time.')
            if job['repair_result_id']:
                original = conn.execute('SELECT payload,observed_at FROM results WHERE id=%s AND worker_id=%s',
                                        (job['repair_result_id'], worker_id)).fetchone()
                if not original or timestamp(body.get('portal_observed_at')) != timestamp(original['payload'].get('portal_observed_at') or original['payload']['observed_at']):
                    raise Conflict('Repair must preserve the original portal observation time.')
                if body['data'] != original['payload']['data']:
                    raise Conflict('PDF repair cannot replace the original portal status fields.')
                if tm['last_published_at'] and original['observed_at'] != tm['last_published_at']:
                    current = False
            latest_observation = tm['last_published_at'] or tm['last_successful_fetch_at']
            if not current or (latest_observation and observed <= latest_observation):
                disposition = 'obsolete'
            elif body['completeness'] == 'partial':
                disposition = 'partial'
                # Stage-aware workers may publish verified core facts while
                # retrying missing documents. Older workers retain strict mode.
                core = (body.get('stages') or {}).get('core', {})
                if core.get('state') == 'complete' and body['data'].get('Class') and (body['data'].get('Application Date') or body['data'].get('Date of Application')):
                    from .facts_projection import publish_facts
                    publish_facts(conn, body, worker_id)
                    if self.settings.publish_legacy:
                        from .legacy_projection import publish
                        publish(conn, body, worker_id, self.settings.artifact_dir)
                    conn.execute('UPDATE trademarks SET last_published_at=%s WHERE app_no=%s', (observed, body['app_no']))
                self._retry(conn, job, 'Partial result: missing required data or panels')
            else:
                disposition = 'accepted'
                from .facts_projection import publish_facts
                publish_facts(conn, body, worker_id)
                if self.settings.publish_legacy:
                    from .legacy_projection import publish
                    publish(conn, {**body, 'observed_at': body.get('portal_observed_at', body['observed_at'])}, worker_id, self.settings.artifact_dir)
                if job['repair_result_id']:
                    conn.execute('UPDATE trademarks SET latest_result_id=%s,last_published_at=%s WHERE app_no=%s', (result_id, observed, body['app_no']))
                    conn.execute("UPDATE jobs SET state='completed',result_id=%s,completed_at=now(),last_error=NULL WHERE id=%s", (result_id, job['id']))
                else:
                    self._advance_schedule(conn, tm, job, body, observed, result_id)
            conn.execute('INSERT INTO results(id,job_id,worker_id,app_no,generation,observed_at,fingerprint,disposition,payload) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                         (result_id, job['id'], worker_id, body['app_no'], body['generation'], observed, fingerprint(body), disposition, Jsonb(body)))
            conn.execute('UPDATE attempts SET finished_at=now(),outcome=%s WHERE reservation_token=%s', (disposition, body['reservation_token']))
            self.audit(conn, worker_id, 'result.' + disposition, {'result_id': result_id, 'job_id': job['id']})
            return {'result_id': result_id, 'disposition': disposition, 'acknowledged': True}

    def _advance_schedule(self, conn, tm, job, body, observed, result_id):
        if tm['automatic_policy']:
            rule = conn.execute('SELECT id FROM policies WHERE lower(match_status)=lower(%s) '
                                'ORDER BY interval_days ASC NULLS LAST,id LIMIT 1', (str(body['data'].get('Status', '')),)).fetchone()
            if rule:
                tm['policy_id'] = rule['id']
        interval = conn.execute('SELECT interval_days FROM policies WHERE id=%s', (tm['policy_id'],)).fetchone()['interval_days']
        next_fetch = observed + dt.timedelta(days=interval) if interval else None
        conn.execute('UPDATE trademarks SET latest_result_id=%s,last_successful_fetch_at=%s,last_published_at=%s,next_fetch_at=%s,policy_id=%s,schedule_override=false WHERE app_no=%s',
                     (result_id, observed, observed, next_fetch, tm['policy_id'], body['app_no']))
        conn.execute("UPDATE jobs SET state='completed',result_id=%s,completed_at=now(),last_error=NULL WHERE id=%s", (result_id, job['id']))

    def request_extraction_repair(self, worker_id, result_id):
        with self.transaction() as conn:
            original = conn.execute("SELECT * FROM results WHERE id=%s AND worker_id=%s AND disposition IN ('accepted','partial')",
                                    (identifier(result_id), worker_id)).fetchone()
            if not original:
                raise Conflict('A saved result belonging to this worker is required.')
            tm = conn.execute('SELECT * FROM trademarks WHERE app_no=%s FOR UPDATE', (original['app_no'],)).fetchone()
            if tm['last_published_at'] != original['observed_at']:
                raise Conflict('Newer data is available; refresh instead of repairing an old observation.')
            open_job = conn.execute("SELECT id FROM jobs WHERE app_no=%s AND state IN ('queued','reserved','retry_wait')", (original['app_no'],)).fetchone()
            if open_job:
                raise Conflict('A refresh is already pending. Let it finish before requesting extraction repair.')
            job_id = self._enqueue(conn, original['app_no'], 200, 'extraction_repair', force=True)
            conn.execute('UPDATE jobs SET repair_result_id=%s,target_worker_id=%s WHERE id=%s', (result_id, worker_id, job_id))
            self.audit(conn, worker_id, 'job.extraction_repair', {'job_id': job_id, 'result_id': result_id})
            return {'job_id': job_id}

    def schedule(self, app_no, policy_id, next_fetch):
        with self.transaction() as conn:
            policy = conn.execute('SELECT * FROM policies WHERE id=%s', (policy_id,)).fetchone()
            if not policy:
                raise ValueError('Unknown policy.')
            if policy['interval_days'] is None:
                next_fetch = None
            conn.execute('INSERT INTO trademarks(app_no,policy_id,next_fetch_at,automatic_policy,schedule_override) VALUES(%s,%s,%s,false,true) '
                         'ON CONFLICT(app_no) DO UPDATE SET policy_id=EXCLUDED.policy_id,next_fetch_at=EXCLUDED.next_fetch_at,automatic_policy=false,schedule_override=true',
                         (application_number(app_no), policy_id, next_fetch))
            self.audit(conn, 'admin', 'schedule.updated', {'app_no': app_no, 'policy_id': policy_id, 'next_fetch_at': str(next_fetch)})

    def create_policy(self, policy_id, name, interval, match_status=None):
        if not isinstance(policy_id, str) or not policy_id.isidentifier() or len(policy_id) > 50 or not isinstance(name, str) or not name.strip():
            raise ValueError('Policy ID and name are required.')
        if interval is not None and (type(interval) is not int or not 1 <= interval <= 36525):
            raise ValueError('Interval must be 1-36525 days, or null for paused.')
        if match_status is not None and (not isinstance(match_status, str) or len(match_status) > 100):
            raise ValueError('Status match must be a string of at most 100 characters.')
        with self.transaction() as conn:
            conn.execute('INSERT INTO policies(id,name,interval_days,match_status) VALUES(%s,%s,%s,%s) ON CONFLICT(id) '
                         'DO UPDATE SET name=EXCLUDED.name,interval_days=EXCLUDED.interval_days,match_status=EXCLUDED.match_status', (policy_id, name, interval, match_status or None))
            conn.execute('UPDATE trademarks SET next_fetch_at=CASE WHEN %s::int IS NULL THEN NULL '
                         'ELSE COALESCE(last_successful_fetch_at,now())+make_interval(days=>%s) END '
                         'WHERE policy_id=%s AND NOT schedule_override', (interval, interval, policy_id))
            self.audit(conn, 'admin', 'policy.updated', {'id': policy_id, 'interval_days': interval})

    # --- Status schedule: "fetch every N days while a trademark is in status X" ----------------
    @staticmethod
    def _clean_status(status):
        if not isinstance(status, str):
            raise ValueError('A portal status is required.')
        status = ' '.join(status.split())
        if not 1 <= len(status) <= 100 or any(ord(ch) < 32 for ch in status):
            raise ValueError('A portal status is required (up to 100 characters).')
        return status

    def status_schedule(self):
        from .statuses import STATUS_DEFAULTS, policy_id
        with self.transaction() as conn:
            rules = {r['match_status'].lower(): r for r in conn.execute(
                'SELECT id,interval_days,match_status FROM policies WHERE match_status IS NOT NULL').fetchall()}
            seen = {r['k']: r for r in conn.execute(
                "SELECT lower(r.payload->'data'->>'Status') AS k, min(r.payload->'data'->>'Status') AS status, count(*) AS n "
                'FROM trademarks t JOIN results r ON r.id=t.latest_result_id '
                "WHERE coalesce(r.payload->'data'->>'Status','')<>'' GROUP BY 1").fetchall()}
            total = conn.execute('SELECT count(*) AS n FROM trademarks').fetchone()['n']
        items, listed = [], set()
        for status, default_days in STATUS_DEFAULTS:
            key = status.lower()
            listed.add(key)
            rule = rules.get(key)
            items.append({'status': status, 'policy_id': rule['id'] if rule else policy_id(status), 'has_rule': bool(rule),
                          'interval_days': rule['interval_days'] if rule else None, 'recommended_days': default_days,
                          'trademarks': seen[key]['n'] if key in seen else 0, 'known': True})
        for key, row in sorted(seen.items(), key=lambda kv: -kv[1]['n']):
            if key not in listed:  # a status the portal returned that is not in the standard list
                rule = rules.get(key)
                items.append({'status': row['status'], 'policy_id': rule['id'] if rule else policy_id(row['status']),
                              'has_rule': bool(rule), 'interval_days': rule['interval_days'] if rule else None,
                              'recommended_days': None, 'trademarks': row['n'], 'known': False})
        return {'items': items, 'total': len(items), 'trademarks_total': total,
                'known_status_trademarks': sum(r['n'] for r in seen.values())}

    def set_status_interval(self, status, interval_days):
        from .statuses import STATUS_DEFAULTS, policy_id
        status = self._clean_status(status)
        if interval_days is not None and (type(interval_days) is not int or not 1 <= interval_days <= 36525):
            raise ValueError('Days must be a whole number from 1 to 36525, or empty to stop automatic fetching.')
        with self.transaction() as conn:
            seen = conn.execute("SELECT 1 FROM trademarks t JOIN results r ON r.id=t.latest_result_id "
                                "WHERE lower(r.payload->'data'->>'Status')=lower(%s) LIMIT 1", (status,)).fetchone()
        if not seen and status.lower() not in {s.lower() for s, _ in STATUS_DEFAULTS}:
            raise ValueError('Unknown portal status. Only the standard statuses and statuses already returned by the portal can be scheduled.')
        pid = policy_id(status)
        with self.transaction() as conn:
            clash = conn.execute('SELECT match_status FROM policies WHERE id=%s', (pid,)).fetchone()
            if clash and (clash['match_status'] or '').lower() != status.lower():
                raise ValueError('This status clashes with an existing policy id; rename that policy first.')
        self.create_policy(pid, 'Status: ' + status, interval_days, status)
        with self.transaction() as conn:
            # Trademarks already sitting in this status follow the new interval immediately. Manual
            # per-trademark schedules (override) are never touched.
            moved = conn.execute(
                'UPDATE trademarks t SET policy_id=%s, next_fetch_at=CASE WHEN %s::int IS NULL THEN NULL '
                'ELSE COALESCE(t.last_successful_fetch_at,now())+make_interval(days=>%s) END '
                "FROM results r WHERE r.id=t.latest_result_id AND lower(r.payload->'data'->>'Status')=lower(%s) "
                'AND t.automatic_policy AND NOT t.schedule_override RETURNING t.app_no',
                (pid, interval_days, interval_days, status)).fetchall()
            self.audit(conn, 'admin', 'status_rule.updated', {'status': status, 'interval_days': interval_days, 'trademarks_moved': len(moved)})
        return {'status': status, 'policy_id': pid, 'interval_days': interval_days, 'updated_trademarks': len(moved)}

    def load_status_defaults(self):
        from .statuses import STATUS_DEFAULTS
        with self.transaction() as conn:
            have = {r['match_status'].lower() for r in conn.execute('SELECT match_status FROM policies WHERE match_status IS NOT NULL').fetchall()}
        created = moved = 0
        for status, days in STATUS_DEFAULTS:
            if status.lower() not in have:
                moved += self.set_status_interval(status, days)['updated_trademarks']
                created += 1
        return {'created': created, 'kept': len(STATUS_DEFAULTS) - created, 'updated_trademarks': moved}

    def list(self, table, limit=50, offset=0):
        allowed = {'workers', 'jobs', 'trademarks', 'policies', 'results', 'audit', 'accounts', 'proceedings', 'document_ledger', 'compliance_events'}
        if table not in allowed:
            raise ValueError('Unknown collection.')
        with self.transaction() as conn:
            order = 'created_at DESC' if table in ('workers', 'jobs', 'trademarks', 'audit', 'accounts') else 'observed_at DESC,app_no' if table == 'document_ledger' else 'id'
            fields = 'id,name,status,capacity,last_contact_at,offline_until,phase,metadata,created_at,enrolled_hostname' if table == 'workers' else '*'
            if table == 'results':
                fields = 'id,job_id,worker_id,app_no,generation,observed_at,received_at,disposition'
            if table == 'accounts':
                fields = 'id,email,worker_id,enabled,created_at'
            rows = conn.execute(f'SELECT {fields} FROM {table} ORDER BY {order} LIMIT %s OFFSET %s', (limit, offset)).fetchall()
            total = conn.execute(f'SELECT count(*) AS total FROM {table}').fetchone()['total']
            return {'items': rows, 'total': total}

    def overview(self):
        with self.transaction() as conn:
            jobs = conn.execute('SELECT state,count(*) AS count FROM jobs GROUP BY state').fetchall()
            due = conn.execute('SELECT count(*) AS n FROM trademarks t LEFT JOIN tm_owner_priority o ON o.app_no=t.app_no '
                               'WHERE t.next_fetch_at<=now() AND (o.app_no IS NULL OR o.has_active_owner)').fetchone()['n']
            contacts = conn.execute("SELECT CASE WHEN metadata->'network'->>'effective_mode'='split' "
                                    "AND last_contact_at>now()-interval '10 minutes' THEN 'online' "
                                    "WHEN offline_until>now() AND phase='scraping' THEN 'expected_offline' "
                                    "WHEN last_contact_at>now()-interval '10 minutes' THEN 'online' ELSE 'unreachable' END AS connectivity, count(*) AS count "
                                    "FROM workers WHERE status<>'retired' GROUP BY 1").fetchall()
            oldest = conn.execute("SELECT min(due_at) AS since FROM jobs WHERE state IN ('queued','retry_wait')").fetchone()['since']
            return {'jobs': {r['state']: r['count'] for r in jobs}, 'overdue': due,
                    'worker_connectivity': {r['connectivity']: r['count'] for r in contacts}, 'oldest_queue_due_at': oldest,
                    'worker_count': conn.execute('SELECT count(*) AS n FROM workers').fetchone()['n']}

    def edit_job(self, job_id, body):
        action = body.get('action')
        priority = body.get('priority', 100)
        if action not in ('retry', 'force_full', 'cancel', 'priority') or type(priority) is not int or not 0 <= priority <= 1000:
            raise ValueError('Choose retry, cancel, or priority (0-1000).')
        with self.transaction() as conn:
            job = conn.execute('SELECT * FROM jobs WHERE id=%s', (job_id,)).fetchone()
            if not job:
                raise ValueError('Job not found.')
            conn.execute('SELECT app_no FROM trademarks WHERE app_no=%s FOR UPDATE', (job['app_no'],))
            job = conn.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
            if action in ('retry', 'force_full'):
                if job['state'] == 'reserved':
                    raise Conflict('This worker still holds a reservation. Drain or cancel it before retrying.')
                if job['state'] in ('queued', 'retry_wait'):
                    conn.execute("UPDATE jobs SET state='queued',available_at=now(),attempts=0,priority=%s,last_error=NULL WHERE id=%s", (priority, job_id))
                else:
                    job_id = self._enqueue(conn, job['app_no'], priority, 'operator_retry', force=True)
                if action == 'force_full':
                    conn.execute('UPDATE jobs SET force_full=true WHERE id=%s', (job_id,))
            elif action == 'cancel':
                if job['state'] == 'completed':
                    raise Conflict('Completed observations cannot be cancelled.')
                conn.execute("UPDATE jobs SET state='cancelled',completed_at=now() WHERE id=%s", (job_id,))
                conn.execute("UPDATE attempts SET finished_at=now(),outcome='cancelled' WHERE job_id=%s AND finished_at IS NULL", (job_id,))
            else:
                conn.execute('UPDATE jobs SET priority=%s WHERE id=%s', (priority, job_id))
            self.audit(conn, 'admin', 'job.' + action, {'job_id': job_id, 'priority': priority})
            return {'job_id': job_id}

    # --- Work queue page -------------------------------------------------------------------
    QUEUE_FILTERS = {
        'attention': "j.state IN ('queued','reserved','retry_wait','failed')",
        'waiting': "j.state='queued'", 'active': "j.state='reserved'", 'retrying': "j.state='retry_wait'",
        'failed': "j.state='failed'", 'done': "j.state='completed'", 'cancelled': "j.state='cancelled'", 'all': 'true',
    }
    PRIORITY_LEVELS = {'normal': 100, 'high': 150, 'top': 500}

    def queue_overview(self, view='attention', limit=50, offset=0):
        if view not in self.QUEUE_FILTERS:
            raise ValueError('Unknown queue view.')
        limit, offset = min(100, max(1, int(limit))), max(0, int(offset))
        where = self.QUEUE_FILTERS[view]
        if view in ('attention', 'all'):  # problems first, then what is being worked on, then what is waiting
            order = ("CASE j.state WHEN 'failed' THEN 0 WHEN 'retry_wait' THEN 1 WHEN 'reserved' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,"
                     'j.priority DESC,j.created_at')
        elif view in ('done', 'cancelled'):
            order = 'j.completed_at DESC NULLS LAST'
        else:
            order = 'j.priority DESC,j.created_at'
        with self.transaction() as conn:
            counts = {r['state']: r['n'] for r in conn.execute('SELECT state,count(*) AS n FROM jobs GROUP BY state').fetchall()}
            done_recently = conn.execute("SELECT count(*) AS n FROM jobs WHERE state='completed' AND completed_at>now()-interval '24 hours'").fetchone()['n']
            total = conn.execute(f'SELECT count(*) AS n FROM jobs j WHERE {where}').fetchone()['n']
            items = conn.execute(
                'SELECT j.id,j.app_no,j.state,j.priority,j.attempts,j.reason,j.last_error,j.created_at,j.available_at,j.reserved_at,'
                "j.completed_at,j.force_full,w.name AS worker_name,r.payload->'data'->>'Trade Mark' AS mark "
                'FROM jobs j LEFT JOIN workers w ON w.id=j.worker_id LEFT JOIN trademarks t ON t.app_no=j.app_no '
                f'LEFT JOIN results r ON r.id=t.latest_result_id WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s', (limit, offset)).fetchall()
        return {'items': items, 'total': total, 'view': view, 'max_attempts': self.settings.max_attempts,
                'counts': {'waiting': counts.get('queued', 0), 'active': counts.get('reserved', 0), 'retrying': counts.get('retry_wait', 0),
                           'failed': counts.get('failed', 0), 'done_recently': done_recently, 'cancelled': counts.get('cancelled', 0)}}

    def queue_trademarks(self, numbers, priority='normal'):
        """Queue many application numbers at once. Staff-requested work goes ahead of scheduled work."""
        if priority not in self.PRIORITY_LEVELS:
            raise ValueError('Choose Normal, High or Top priority.')
        parts = re.split(r'[\s,;]+', numbers.strip()) if isinstance(numbers, str) else [str(x).strip() for x in (numbers or [])]
        parts = [p for p in parts if p]
        if not parts:
            raise ValueError('Enter at least one application number.')
        if len(parts) > 500:
            raise ValueError('Queue up to 500 trademarks at a time.')
        queued, moved_up, invalid, seen = [], [], [], set()
        for number in parts:
            if number in seen:
                continue
            seen.add(number)
            try:
                application_number(number)
            except ValueError:
                invalid.append(number)
                continue
            with self.transaction() as conn:
                waiting = conn.execute("SELECT 1 FROM jobs WHERE app_no=%s AND state IN ('queued','reserved','retry_wait')", (number,)).fetchone()
            self.enqueue(number, self.PRIORITY_LEVELS[priority], 'staff_request')
            (moved_up if waiting else queued).append(number)
        return {'queued': queued, 'already_waiting': moved_up, 'invalid': invalid}

    def reconcile(self, worker_id):
        with self.transaction() as conn:
            jobs = conn.execute("SELECT id,generation,reservation_token,state,expires_at FROM jobs WHERE worker_id=%s AND state='reserved'", (worker_id,)).fetchall()
            return {'assignments': jobs}

    def add_account(self, body):
        from cryptography.fernet import Fernet
        if not self.settings.account_key:
            raise ValueError('Configure CONTROL_ACCOUNT_KEY before storing portal credentials.')
        email, password = body.get('email'), body.get('app_password')
        if not isinstance(email, str) or '@' not in email or not isinstance(password, str) or not password:
            raise ValueError('Email and app password are required.')
        email = email.strip().lower()
        worker_id = identifier(body['worker_id']) if body.get('worker_id') else None
        credentials = {'email_address': email, 'app_password': password,
                       'imap_server': body.get('imap_server', 'imap.gmail.com'), 'imap_port': 993}
        encrypted = Fernet(self.settings.account_key.encode()).encrypt(json.dumps(credentials).encode()).decode()
        with self.transaction() as conn:
            account_id = uid()
            conn.execute('INSERT INTO accounts(id,email,encrypted_credentials,worker_id) VALUES(%s,%s,%s,%s)',
                         (account_id, email, encrypted, worker_id))
            self.audit(conn, 'admin', 'account.created', {'id': account_id, 'email': email})
            return account_id

    def assign_account(self, account_id, body):
        worker_id = identifier(body['worker_id']) if body.get('worker_id') else None
        enabled = body.get('enabled', True)
        if type(enabled) is not bool:
            raise ValueError('Enabled must be a boolean.')
        with self.transaction() as conn:
            old = conn.execute('SELECT * FROM accounts WHERE id=%s FOR UPDATE', (account_id,)).fetchone()
            if not old:
                raise ValueError('Account not found.')
            if old['worker_id']:
                conn.execute('SELECT id FROM workers WHERE id=%s FOR UPDATE', (old['worker_id'],))
            # Moving credentials while an offline batch owns them could cause
            # concurrent portal logins. Drain that machine first.
            if old['worker_id'] and conn.execute("SELECT id FROM jobs WHERE worker_id=%s AND state='reserved' LIMIT 1", (old['worker_id'],)).fetchone():
                raise Conflict('Drain the current worker before moving/disabling its account.')
            conn.execute('UPDATE accounts SET worker_id=%s,enabled=%s WHERE id=%s',
                         (worker_id, enabled, account_id))
            self.audit(conn, 'admin', 'account.assigned', {'id': account_id, 'worker_id': body.get('worker_id')})

    def accounts_overview(self):
        """Accounts with their machine's NAME, plus the machines they can be given to."""
        with self.transaction() as conn:
            accounts = conn.execute(
                'SELECT a.id,a.email,a.worker_id,w.name AS worker_name,a.enabled,a.created_at '
                'FROM accounts a LEFT JOIN workers w ON w.id=a.worker_id ORDER BY a.created_at DESC,a.email').fetchall()
            machines = conn.execute(
                "SELECT w.id,w.name,w.status,w.capacity,count(a.id) AS accounts,count(a.id) FILTER (WHERE a.enabled) AS enabled_accounts "
                "FROM workers w LEFT JOIN accounts a ON a.worker_id=w.id WHERE w.status NOT IN ('retired','pending') GROUP BY w.id ORDER BY w.name").fetchall()
        assigned = sum(1 for a in accounts if a['worker_id'])
        return {'accounts': accounts, 'machines': machines, 'total': len(accounts), 'assigned': assigned,
                'unassigned': len(accounts) - assigned, 'disabled': sum(1 for a in accounts if not a['enabled'])}

    def assign_accounts(self, account_ids, worker_id, enabled=None, keep_machine=False):
        """Give several accounts to one machine (or take them all back). Reports per account.
        keep_machine=True leaves every account on its current machine (used to enable/disable)."""
        if not isinstance(account_ids, list) or not account_ids or len(account_ids) > 500:
            raise ValueError('Choose at least one account.')
        if enabled is not None and type(enabled) is not bool:
            raise ValueError('Enabled must be true or false.')
        machine = None
        if worker_id and not keep_machine:
            worker_id = identifier(worker_id)
            with self.transaction() as conn:
                machine = conn.execute('SELECT id,name,status FROM workers WHERE id=%s', (worker_id,)).fetchone()
            if not machine:
                raise ValueError('That machine does not exist.')
            if machine['status'] == 'retired':
                raise ValueError('That machine has been removed. Choose another one.')
            if machine['status'] == 'pending':
                raise ValueError('That machine is waiting for approval. Approve it in Fleet machines first.')
        done, failed = [], []
        for account_id in account_ids:
            try:
                account_id = identifier(account_id)
                with self.transaction() as conn:
                    row = conn.execute('SELECT email,enabled,worker_id FROM accounts WHERE id=%s', (account_id,)).fetchone()
                if not row:
                    raise ValueError('This account no longer exists.')
                self.assign_account(account_id, {'worker_id': (row['worker_id'] if keep_machine else worker_id) or None,
                                                 'enabled': row['enabled'] if enabled is None else enabled})
                done.append(account_id)
            except Conflict:
                failed.append({'id': account_id, 'reason': 'Its current machine is busy with a batch. Use "Finish and stop" on that machine in Fleet machines, then try again.'})
            except ValueError as exc:
                failed.append({'id': account_id, 'reason': str(exc)})
        return {'assigned': done, 'failed': failed, 'machine': machine['name'] if machine else None}

    def configuration(self, worker_id):
        from cryptography.fernet import Fernet
        with self.transaction() as conn:
            rows = conn.execute('SELECT id,encrypted_credentials FROM accounts WHERE worker_id=%s AND enabled', (worker_id,)).fetchall()
            if rows and not self.settings.account_key:
                raise ValueError('Portal credential encryption key is unavailable.')
            accounts = [{**json.loads(Fernet(self.settings.account_key.encode()).decrypt(r['encrypted_credentials'].encode())), 'id': r['id']} for r in rows]
            return {'schema_version': 1, 'accounts': accounts, 'reservation_seconds': self.settings.reservation_seconds}
