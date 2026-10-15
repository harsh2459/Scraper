"""Read-only Contabo inventory. Never imports the app or displays env values.

Run with the server's system Python as root. Output contains schema metadata,
sizes, aggregate counts and service status, not passwords or customer records.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


def run(command, timeout=30):
    try:
        reply = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return {'ok': reply.returncode == 0, 'output': reply.stdout.strip(),
                'error': reply.stderr.strip()[:1000] if reply.returncode else None}
    except FileNotFoundError:
        return {'ok': False, 'error': 'Command not installed.'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'Read-only inspection timed out.'}


def configured_keys(path):
    allowed = ('PG_HOST', 'PG_PORT', 'PG_DBNAME', 'PG_DATABASE', 'PG_USER', 'PG_PASSWORD',
               'GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET',
               'GOOGLE_DRIVE_REFRESH_TOKEN', 'GOOGLE_DRIVE_ROOT_FOLDER_ID',
               'TM_DATABASE_URL', 'CONTROL_DATABASE_URL')
    result = {key: False for key in allowed}
    if path.is_file():
        for raw in path.read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                if key.strip() in result:
                    result[key.strip()] = bool(value.strip().strip('"').strip("'"))
    return result


def nginx_routes(directory):
    output = []
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                match = re.match(r'^\s*(server_name|listen|root|proxy_pass)\s+([^;]+);', line)
                if not match:
                    continue
                directive, value = match.groups()
                if directive == 'proxy_pass':
                    try:
                        parsed = urlsplit(value)
                        value = parsed.scheme + '://' + (parsed.hostname or 'unknown')
                        if parsed.port:
                            value += ':' + str(parsed.port)
                        value += parsed.path
                    except ValueError:
                        value = '[unparsed proxy target omitted]'
                output.append({'site': path.name, 'directive': directive, 'value': value})
    return output


SCHEMA_QUERY = """
BEGIN READ ONLY;
SET LOCAL statement_timeout = '20s';
SELECT jsonb_build_object(
  'database', current_database(),
  'postgres_version', current_setting('server_version'),
  'database_size_bytes', pg_database_size(current_database()),
  'schemas', (SELECT jsonb_agg(nspname ORDER BY nspname) FROM pg_namespace
             WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'),
  'tables', (SELECT jsonb_agg(jsonb_build_object(
             'schema', schemaname, 'table', relname, 'estimated_rows', n_live_tup,
             'size_bytes', pg_total_relation_size(relid)) ORDER BY schemaname, relname)
             FROM pg_stat_user_tables),
  'columns', (SELECT jsonb_agg(jsonb_build_object(
             'schema', table_schema, 'table', table_name, 'column', column_name,
             'type', data_type, 'nullable', is_nullable) ORDER BY table_schema, table_name, ordinal_position)
             FROM information_schema.columns WHERE table_schema IN ('public','fleet_control')),
  'constraints', (SELECT jsonb_agg(jsonb_build_object(
             'schema', table_schema, 'table', table_name, 'name', constraint_name,
             'type', constraint_type) ORDER BY table_schema, table_name, constraint_name)
             FROM information_schema.table_constraints WHERE table_schema IN ('public','fleet_control'))
);
COMMIT;
"""

DRIVE_COUNTS_QUERY = """
BEGIN READ ONLY;
SET LOCAL statement_timeout = '20s';
WITH documents AS (
  SELECT 'tma_documents' AS kind, drive_file_id, local_deleted_at
  FROM public.tma_documents WHERE source_pdf_path IS NOT NULL
  UNION ALL
  SELECT 'ipindia_reminders', drive_file_id, local_deleted_at
  FROM public.ipindia_reminders WHERE document_path IS NOT NULL
  UNION ALL
  SELECT 'ipindia_correspondence', drive_file_id, local_deleted_at
  FROM public.ipindia_correspondence WHERE document_path IS NOT NULL
), counts AS (
  SELECT kind, count(*) AS records,
    count(*) FILTER (WHERE drive_file_id IS NULL) AS no_drive_id,
    count(*) FILTER (WHERE drive_file_id IS NOT NULL AND local_deleted_at IS NULL) AS cleanup_pending
  FROM documents GROUP BY kind
)
SELECT jsonb_agg(to_jsonb(counts) ORDER BY kind) FROM counts;
COMMIT;
"""


def postgres(query, database):
    result = run(['sudo', '-n', '-u', 'postgres', 'psql', '-X', '-q', '-A', '-t',
                  '-v', 'ON_ERROR_STOP=1', '-d', database, '-c', query], timeout=30)
    if result['ok']:
        try:
            return {'ok': True, 'data': json.loads(result['output'])}
        except ValueError:
            return {'ok': False, 'error': 'Unexpected metadata response; no raw output included.'}
    return result


def inspect(root, database):
    units = ('tmtracker-scraper', 'tmtracker-pdf-scraper', 'tmtracker-backend',
             'tmtracker-control', 'tmtracker-user-fleet', 'nginx', 'postgresql', 'docker')
    services = {unit: run(['systemctl', 'show', unit, '--no-pager',
                          '-p', 'ActiveState', '-p', 'SubState', '-p', 'User',
                          '-p', 'Group', '-p', 'WorkingDirectory', '-p', 'FragmentPath']) for unit in units}
    files = {}
    for relative in ('app.py', 'db.py', 'google_drive.py', 'pdf_scraper/pdf_processor.py',
                     'tm_manager/app.py', 'tm_manager/db.py', 'scripts/backfill_drive_migration.py'):
        path = root / relative
        if path.is_file():
            files[relative] = {'bytes': path.stat().st_size,
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return {
        'observed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'inspection': 'read_only; no app imports, config changes, migrations, uploads or deletion',
        'disk': run(['df', '-h', '/']),
        'application_folders': run(['du', '-xh', '--max-depth=2', str(root)], timeout=120),
        'services': services,
        'docker_version': run(['docker', '--version']),
        'compose_version': run(['docker', 'compose', 'version', '--short']),
        'nginx_routes': nginx_routes(Path('/etc/nginx/sites-enabled')),
        'config_presence_only': {'.env': configured_keys(root / '.env'),
                                 'tm_manager/.env': configured_keys(root / 'tm_manager' / '.env')},
        'source_fingerprints': files,
        'postgres_metadata': postgres(SCHEMA_QUERY, database),
        'document_drive_counts': postgres(DRIVE_COUNTS_QUERY, database),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/tmtracker'))
    parser.add_argument('--database', default='datascraper')
    args = parser.parse_args()
    print(json.dumps(inspect(args.root, args.database), indent=2))


if __name__ == '__main__':
    main()
