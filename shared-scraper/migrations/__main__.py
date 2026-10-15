import hashlib
from pathlib import Path
import psycopg
from control_plane.config import Settings


def migrate(database_url):
    with psycopg.connect(database_url) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(84101732)')
        conn.execute('CREATE TABLE IF NOT EXISTS public.fleet_schema_migrations '
                     '(version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
        for path in sorted(Path(__file__).parent.glob('[0-9]*.sql')):
            sql = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            row = conn.execute('SELECT checksum FROM public.fleet_schema_migrations WHERE version=%s', (path.name,)).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f'Applied migration changed: {path.name}')
                continue
            conn.execute(sql)
            conn.execute('INSERT INTO public.fleet_schema_migrations(version,checksum) VALUES(%s,%s)', (path.name, checksum))
            print(f'Applied {path.name}')


if __name__ == '__main__':
    migrate(Settings.from_env().database_url)
