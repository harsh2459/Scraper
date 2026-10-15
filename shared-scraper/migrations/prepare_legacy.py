"""Explicitly prepare a disposable legacy schema without importing apps.

Only development/test databases are permitted. Production already has these
tables; its rollout uses additive numbered migrations, not this helper.
"""
import ast
from pathlib import Path
from urllib.parse import urlparse
import psycopg


def prepare(database_url):
    parsed = urlparse(database_url)
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or not (parsed.path.startswith('/fleet_test') or parsed.path.startswith('/fleet_dev')):
        raise ValueError('Legacy fixture preparation is limited to local fleet_test/fleet_dev databases.')
    root = Path(__file__).parent.parent
    with psycopg.connect(database_url) as conn:
        for filename in ('db.py', 'tm_manager/db.py'):
            tree = ast.parse((root / filename).read_text(encoding='utf-8-sig'))
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.endswith('_SCHEMA'):
                    conn.execute(ast.literal_eval(node.value))


if __name__ == '__main__':
    from control_plane.config import Settings
    prepare(Settings.from_env().database_url)
