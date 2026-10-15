"""Restore the private schema into a NEW loopback-only rehearsal database.

No live-server connections, real customer data, legacy app imports or cleanup.
The database is retained for inspection; its generated name is printed.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'shared-scraper'))
from migrations.__main__ import migrate
from control_plane.config import Settings
from control_plane.contracts import digest
from control_plane.repository import Repository


def snapshot(conn, tables):
    return {table: conn.execute(sql.SQL('SELECT * FROM public.{} ORDER BY id').format(
        sql.Identifier(table))).fetchall() for table in tables}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', type=Path, default=ROOT / '.runtime/contabo-schema.sql')
    parser.add_argument('--report', type=Path, default=ROOT / '.runtime/contabo-migration-rehearsal.json')
    args = parser.parse_args()
    original = args.schema.read_bytes()
    dump = original.decode('utf-8-sig')
    if not re.search(r'^CREATE SCHEMA public;$', dump, re.MULTILINE):
        raise ValueError('Expected the public schema-only export.')
    if re.search(r'^(COPY|INSERT INTO) ', dump, re.MULTILINE):
        raise ValueError('Only a schema-only export is permitted.')
    # psql restrict directives are not SQL; remove only those exact directives.
    dump = re.sub(r'^\\(?:un)?restrict [A-Za-z0-9]+\r?\n', '', dump, flags=re.MULTILINE)
    if re.search(r'^\\', dump, re.MULTILINE):
        raise ValueError('Unexpected psql command in schema export.')
    # --schema=public excludes extension declarations but includes indexes using
    # gin_trgm_ops. Bootstrap that prerequisite in this new local database only.
    dump = dump.replace('CREATE SCHEMA public;',
                        'CREATE SCHEMA public; CREATE EXTENSION pg_trgm WITH SCHEMA public;', 1)
    name = 'fleet_test_contabo_' + uuid.uuid4().hex[:12]
    admin_url = 'postgresql://fleet_test_admin@127.0.0.1:55439/fleet_test_control'
    database_url = 'postgresql://fleet_test_admin@127.0.0.1:55439/' + name
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    with psycopg.connect(database_url) as conn:
        # The default schema is empty in this newly created database.
        conn.execute('DROP SCHEMA public')
        conn.execute(dump)
        conn.execute('SET search_path TO public')
        version = conn.execute('SHOW server_version').fetchone()[0]
        legacy_tables = conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0]
        user_id = conn.execute("INSERT INTO users(email,password_hash,is_admin) VALUES('rehearsal@example.invalid','synthetic-non-login-hash',true) RETURNING id").fetchone()[0]
        search_id = conn.execute("INSERT INTO ipindia_results(query,search_type,data_json,error) VALUES('7654321','appno_batch',%s,'') RETURNING id",
                                 (Jsonb([{'Status': 'Registered', 'Class': '35'}]),)).fetchone()[0]
        conn.execute("INSERT INTO my_trademarks(user_id,app_no,notes,status,last_search_id,fetched_at,drive_folder_id) VALUES(%s,'7654321','private synthetic note','fetched',%s,now(),'synthetic-folder-id')", (user_id, search_id))
        conn.execute("INSERT INTO tma_documents(app_no,search_id,source_pdf_filename,source_pdf_path,raw_json,drive_file_id) VALUES('7654321',%s,'synthetic.pdf','/opt/tmtracker/data/ipindia/synthetic.pdf',%s,'synthetic-drive-file-id')",
                     (search_id, Jsonb({'class': {'number': '35'}})))
        notice = conn.execute("INSERT INTO ipindia_reminders(app_no,search_id,corres_no,subject,document_path,drive_file_id) VALUES('7654321',%s,'synthetic-notice','Notice of Opposition','synthetic-notice.pdf','synthetic-notice-drive-id') RETURNING id", (search_id,)).fetchone()[0]
        conn.execute("INSERT INTO compliance_status(source,source_id,user_id,status) VALUES('reminder',%s,%s,'completed')", (notice, user_id))
        tables = ('users', 'my_trademarks', 'ipindia_results', 'ipindia_reminders', 'compliance_status')
        before = snapshot(conn, tables)
        document_before = conn.execute('SELECT id,source_pdf_path,drive_file_id,raw_json FROM tma_documents').fetchall()
    migrate(database_url)
    with psycopg.connect(database_url) as conn:
        assert snapshot(conn, tables) == before, 'Migration changed original rows'
        assert conn.execute('SELECT id,source_pdf_path,drive_file_id,raw_json FROM tma_documents').fetchall() == document_before
        assert conn.execute("SELECT count(*) FROM fleet_control.trademarks WHERE app_no='7654321'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM fleet_control.field_values WHERE app_no='7654321' AND field_key='class.number'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM public.fleet_event_preferences WHERE user_id=%s AND status='completed'", (user_id,)).fetchone()[0] == 1
        versions = conn.execute('SELECT version FROM public.fleet_schema_migrations ORDER BY version').fetchall()
    migrate(database_url)
    with psycopg.connect(database_url) as conn:
        assert conn.execute('SELECT version FROM public.fleet_schema_migrations ORDER BY version').fetchall() == versions
        assert snapshot(conn, tables) == before
    settings = Settings(database_url, ROOT / '.runtime/contabo-rehearsal-artifacts' / name,
                        'synthetic-rehearsal-admin-credential', publish_legacy=True)
    repo = Repository(settings)
    worker = repo.create_worker('schema-rehearsal-worker')
    content = b'Synthetic PDF artifact; no real document or Google Drive request.'
    sha = digest(content)
    for _ in range(2):
        repo.enqueue('7654321')
        assignment = repo.claim(worker['id'], 1)[0]
        result_id = str(uuid.uuid4())
        directory = settings.artifact_dir / worker['id'] / result_id
        directory.mkdir(parents=True)
        (directory / sha).write_bytes(content)
        repo.record_artifact(worker['id'], result_id, sha, len(content))
        result = {'schema_version': 1, 'result_id': result_id,
                  'job_id': assignment['id'], 'reservation_token': assignment['reservation_token'],
                  'app_no': assignment['app_no'], 'generation': assignment['generation'],
                  'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'data': {'Status': 'Registered', 'Class': '35', 'Application Date': '01/01/2026', 'Trade Mark': 'SYNTHETIC'},
                  'artifacts': [{'sha256': sha, 'size': len(content), 'filename': 'synthetic-new.pdf', 'kind': 'document'}],
                  'extracted': [{'sha256': sha, 'fields': {'class': {'number': '35'}}}],
                  'warnings': [], 'completeness': 'complete'}
        reply = repo.complete(worker['id'], result)
        assert reply['acknowledged'] and reply['disposition'] == 'accepted'
        assert repo.complete(worker['id'], result)['acknowledged'], 'Lost-ack retry failed'
    with psycopg.connect(database_url) as conn:
        assert conn.execute('SELECT id,source_pdf_path,drive_file_id,raw_json FROM tma_documents WHERE drive_file_id IS NOT NULL').fetchall() == document_before
        assert conn.execute("SELECT user_id,notes,drive_folder_id,status FROM my_trademarks WHERE app_no='7654321'").fetchone() == (user_id, 'private synthetic note', 'synthetic-folder-id', 'fetched')
        assert conn.execute('SELECT count(*) FROM tma_documents WHERE fleet_sha256=%s', (sha,)).fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM tma_documents WHERE fleet_canonical').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM tm_status_history WHERE app_no='7654321'").fetchone()[0] == 2
    report = {'observed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'database': name, 'host': '127.0.0.1', 'port': 55439,
              'schema_sha256': hashlib.sha256(original).hexdigest(),
              'source_version': '16.15', 'test_version': version,
              'legacy_table_count': legacy_tables, 'migrations': [row[0] for row in versions],
              'checks': ['schema restore including actual indexes/constraints',
                         'synthetic owners/notes/Drive IDs/document paths preserved',
                         'existing fetched application schedule backfilled',
                         'extracted field provenance backfilled',
                         'existing private compliance status preserved',
                         'second migration run unchanged',
                         'two new coordinator deliveries publish to actual legacy schema',
                         'lost acknowledgement retries are idempotent',
                         'repeated PDF extraction keeps stable compatibility rows',
                         'historical Drive references unchanged after new delivery'],
              'limitations': ['Synthetic rows only; no real customer data tested.',
                              'Local PostgreSQL major version differs from production.',
                              'No live VPN, Google Drive, container or public routing tested.'],
              'status': 'passed'}
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Rehearsal passed. Local database:', name)
    print('Report:', args.report)


if __name__ == '__main__':
    main()
