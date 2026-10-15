import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('contabo_bundle', Path(__file__).resolve().parents[2] / 'deploy/build_contabo_bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class BundleTests(unittest.TestCase):
    def test_release_selection_excludes_private_runtime_and_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            required = ('.dockerignore', 'deploy/fleet-service-grants.sql',
                        'shared-scraper/tm_manager/scripts/parse_journal.py',
                        'shared-scraper/tm_manager/scripts/auto_fetch_journal.py',
                        'deploy/containers/Dockerfile', 'shared-scraper/tm_manager/frontend/src/App.tsx',
                        'shared-scraper/control_plane/app.py')
            private = ('.runtime/drive-auth/authorization.json', 'shared-scraper/.env',
                       'shared-scraper/vpn-configs/tunnel.conf', 'shared-scraper/data/file.pdf',
                       'shared-scraper/tm_manager/frontend/node_modules/private/index.ts',
                       'shared-scraper/tm_manager/frontend/dist/index.html',
                       'shared-scraper/tm_manager/frontend/.env', 'deploy/HOW-TO-UPDATE.md')
            for name in required + private:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic', encoding='utf-8')
            selected = {p.relative_to(root).as_posix() for p in bundle.release_files(root)}
            self.assertTrue(set(required).issubset(selected))
            self.assertFalse(set(private) & selected)


if __name__ == '__main__':
    unittest.main()
