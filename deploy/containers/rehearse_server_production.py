"""Rehearse release migrations on the separate PostgreSQL 16 staging clone."""
import os
from pathlib import Path
import pwd
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'shared-scraper'))
from migrations.__main__ import migrate
import psycopg


def snapshot(conn):
    tables = ('users','my_trademarks','tma_documents','ipindia_reminders','ipindia_correspondence',
              'compliance_status','tm_status_history','custom_tasks','subscriptions','plans')
    expression = "md5((to_jsonb(t)-'fleet_canonical'-'fleet_sha256')::text)"
    return {table:conn.execute(f"SELECT count(*),md5(coalesce(string_agg({expression},'' ORDER BY {expression}),'')) FROM public.{table} t").fetchone() for table in tables}


if __name__ == '__main__':
    if pwd.getpwuid(os.geteuid()).pw_name != 'postgres':
        raise SystemExit('Run as the postgres OS account.')
    # This helper never migrates the live DB.
    url = 'postgresql:///fleet_staging_datascraper?host=/var/run/postgresql&user=postgres'
    with psycopg.connect(url) as conn:
        if not conn.execute('SHOW server_version').fetchone()[0].startswith('16.'):
            raise SystemExit('Expected PostgreSQL 16 staging.')
        before = snapshot(conn)
    migrate(url)
    migrate(url)
    with psycopg.connect(url) as conn:
        if snapshot(conn) != before:
            raise SystemExit('Staging original-record verification failed.')
    print('PostgreSQL 16 staging rehearsal: migrations, repeat run and original records PASS.')
