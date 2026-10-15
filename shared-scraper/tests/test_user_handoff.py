"""Exercise the real user API against a disposable local database.

Loaded in its own process to avoid legacy modules named config/db colliding
with the control-plane modules. No services/background threads are started.
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest

from migrations.prepare_legacy import prepare
from tests import test_fleet as fleet


class UserHandoffTests(unittest.TestCase):
    setUpClass = classmethod(fleet.FleetTests.setUpClass.__func__)
    setUp = fleet.FleetTests.setUp
    def test_user_api_durable_handoff_projection_and_permissions(self):
        prepare(self.url)
        script = Path(__file__).parent / 'verify_user_handoff.py'
        env = {**os.environ, 'TM_DATABASE_URL': self.url, 'TM_LOAD_LEGACY_ENV': '0', 'TM_FLEET_CONTROL_ENABLED': '1',
               'TM_AUTO_MIGRATE': '0', 'TM_ENV': 'development', 'COOKIE_SECURE': '0',
               'SENTRY_DSN_BACKEND': '', 'CONTROL_ARTIFACT_DIR': str(self.settings.artifact_dir)}
        response = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)


if __name__ == '__main__':
    unittest.main()
