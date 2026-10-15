import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TM_MANAGER = Path(__file__).resolve().parents[1] / 'tm_manager'


class UserAppImportTest(unittest.TestCase):
    def test_app_imports_with_only_script_directory_on_path(self):
        # The container runs `python /opt/tmtracker/tm_manager/app.py`, so sys.path[0]
        # is tm_manager/ and nothing else from the repo. Tests run from the repo root
        # hide a missing path entry for shared modules such as fleet_storage.
        code = textwrap.dedent(f'''
            import runpy, sys
            sys.path.insert(0, {str(TM_MANAGER)!r})
            runpy.run_path({str(TM_MANAGER / 'app.py')!r}, run_name='not_main')
            ''')
        env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, '-c', code], cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=120)
        self.assertNotIn('ModuleNotFoundError', result.stderr, result.stderr[-1500:])


if __name__ == '__main__':
    unittest.main()
