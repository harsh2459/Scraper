import json
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from control_plane.contracts import digest


class LocalStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'worker.sqlite3'
        with self.connect() as conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assignments (
                    key TEXT PRIMARY KEY, job_id TEXT NOT NULL, token TEXT NOT NULL,
                    assignment TEXT NOT NULL, state TEXT NOT NULL, result TEXT, paths TEXT,
                    error TEXT, disposition TEXT, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at REAL NOT NULL,message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS document_cache (
                    app_no TEXT NOT NULL, source_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    artifact TEXT NOT NULL, path TEXT NOT NULL, fields TEXT, events TEXT NOT NULL,
                    checked_at REAL NOT NULL, parser_version TEXT NOT NULL,
                    PRIMARY KEY(app_no,source_key)
                );
            ''')
            conn.execute("UPDATE assignments SET state='received' WHERE state='scraping'")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=FULL')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def receive(self, jobs):
        with self.connect() as conn:
            for job in jobs:
                key = job['id'] + ':' + job['reservation_token']
                conn.execute('INSERT OR IGNORE INTO assignments(key,job_id,token,assignment,state,updated_at) VALUES(?,?,?,?,?,?)',
                             (key, job['id'], job['reservation_token'], json.dumps(job), 'received', time.time()))

    def list(self, states=None, limit=100):
        with self.connect() as conn:
            where = 'WHERE state IN (' + ','.join('?' for _ in states) + ')' if states else ''
            rows = conn.execute(f'SELECT * FROM assignments {where} ORDER BY updated_at LIMIT ?', (*states, limit) if states else (limit,)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                for name in ('assignment', 'result', 'paths'):
                    item[name] = json.loads(item[name]) if item[name] else None
                items.append(item)
            return items

    def transition(self, key, state, error=None, disposition=None):
        with self.connect() as conn:
            conn.execute('UPDATE assignments SET state=?,error=?,disposition=?,updated_at=? WHERE key=?',
                         (state, error, disposition, time.time(), key))

    def has_result(self, key):
        with self.connect() as conn:
            row = conn.execute('SELECT result FROM assignments WHERE key=?', (key,)).fetchone()
            return bool(row and row['result'])

    def cache_document(self, app_no, source_key, fingerprint, artifact, path, fields, events, parser_version):
        with self.connect() as conn:
            conn.execute('INSERT INTO document_cache VALUES(?,?,?,?,?,?,?,?,?) '
                         'ON CONFLICT(app_no,source_key) DO UPDATE SET fingerprint=excluded.fingerprint,artifact=excluded.artifact,'
                         'path=excluded.path,fields=excluded.fields,events=excluded.events,checked_at=excluded.checked_at,parser_version=excluded.parser_version',
                         (app_no, source_key, fingerprint, json.dumps(artifact), str(path), json.dumps(fields),
                          json.dumps(events), time.time(), parser_version))

    def result_by_id(self, result_id):
        with self.connect() as conn:
            row = conn.execute('SELECT result,paths FROM assignments WHERE json_extract(result,\'$.result_id\')=?', (result_id,)).fetchone()
        if not row:
            raise ValueError('Saved local result not found')
        return json.loads(row['result']), json.loads(row['paths'])

    def cached_document(self, app_no, source_key, fingerprint, max_age_days=30):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM document_cache WHERE app_no=? AND source_key=? AND fingerprint=?',
                               (app_no, source_key, fingerprint)).fetchone()
        if not row or time.time() - row['checked_at'] >= max_age_days * 86400:
            return None
        item = dict(row)
        for name in ('artifact', 'fields', 'events'):
            item[name] = json.loads(item[name])
        path = Path(item['path'])
        if not path.is_file() or digest(path.read_bytes()) != item['artifact']['sha256']:
            return None
        return item

    def save_result(self, key, result, paths):
        # The portal result is durable before any network delivery begins.
        with self.connect() as conn:
            conn.execute("UPDATE assignments SET state='upload_pending',result=?,paths=?,error=NULL,updated_at=? WHERE key=?",
                         (json.dumps(result), json.dumps(paths), time.time(), key))

    def save_artifact(self, result_id, name, content, kind):
        sha = digest(content)
        directory = self.root / 'artifacts' / result_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / sha
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
            temp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return {'sha256': sha, 'filename': name, 'size': len(content), 'kind': kind}, str(target)

    def set(self, key, value):
        with self.connect() as conn:
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def get(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def event(self, message):
        with self.connect() as conn:
            conn.execute('INSERT INTO events(at,message) VALUES(?,?)', (time.time(), message[:2000]))
            conn.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)')

    def events(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT 50')]

    def counts(self):
        with self.connect() as conn:
            return {r['state']: r['n'] for r in conn.execute('SELECT state,count(*) AS n FROM assignments GROUP BY state')}
