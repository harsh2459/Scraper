import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[2] / 'deploy/containers/staging_entrypoint.py'
spec = importlib.util.spec_from_file_location('staging_guard', path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class StagingGuardTests(unittest.TestCase):
    def test_live_database_and_remote_hosts_are_refused(self):
        for url in ('postgresql://role@127.0.0.1/datascraper',
                    'postgresql://role@62.146.239.158/fleet_staging_datascraper',
                    'postgresql://role@127.0.0.1/fleet_test_control', ''):
            with self.assertRaises(ValueError):
                guard.validate('control', {'CONTROL_DATABASE_URL': url})

    def test_user_requires_disabled_background_work_and_migrations(self):
        env = {'TM_DATABASE_URL': 'postgresql://role@127.0.0.1/fleet_staging_datascraper',
               'TM_AUTO_MIGRATE': '0', 'TM_BACKGROUND_TASKS_ENABLED': '0',
               'TM_BIND_HOST': '127.0.0.1', 'SECRET_KEY': 'synthetic-session-secret'}
        guard.validate('user', env)
        for key, value in (('TM_AUTO_MIGRATE', '1'), ('TM_BACKGROUND_TASKS_ENABLED', '1'),
                           ('TM_BIND_HOST', '0.0.0.0'), ('SECRET_KEY', '')):
            with self.assertRaises(ValueError):
                guard.validate('user', {**env, key: value})

    def test_only_explicit_operations_are_allowed(self):
        env = {'CONTROL_DATABASE_URL': 'postgresql://role@127.0.0.1/fleet_staging_datascraper'}
        for operation in ('control', 'migrate'):
            guard.validate(operation, env)
        with self.assertRaises(ValueError):
            guard.validate('shell', env)


if __name__ == '__main__':
    unittest.main()
