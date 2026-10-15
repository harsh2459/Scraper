import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('drive_account_repair', Path(__file__).resolve().parents[2] / 'deploy' / 'drive_account_repair.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class DriveAccountRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.values = dict(zip(repair.KEYS, ('synthetic-client', 'synthetic-secret', 'synthetic-refresh')))

    def test_replaces_duplicate_credentials_and_preserves_other_settings(self):
        original = ('PG_PASSWORD=synthetic-database-password\nGOOGLE_DRIVE_ROOT_FOLDER_ID=unchanged-folder\n'
                    'GOOGLE_DRIVE_REFRESH_TOKEN=old-first\nGOOGLE_DRIVE_REFRESH_TOKEN=old-second\n'
                    '# keep this comment\nOTHER_SETTING=yes\n')
        updated = repair.replace_auth(original, self.values)
        self.assertIn('PG_PASSWORD=synthetic-database-password', updated)
        self.assertIn('GOOGLE_DRIVE_ROOT_FOLDER_ID=unchanged-folder', updated)
        self.assertIn('# keep this comment', updated)
        self.assertIn('OTHER_SETTING=yes', updated)
        self.assertNotIn('old-first', updated)
        self.assertNotIn('old-second', updated)
        self.assertEqual(updated.count('GOOGLE_DRIVE_REFRESH_TOKEN='), 1)

    def test_bundle_rejects_missing_fields_and_line_injection(self):
        for value in ('', 'token\nOTHER_SETTING=bad', 'token\x00'):
            with self.subTest(value=value), self.assertRaises(repair.RepairError):
                repair.replace_auth('', {**self.values, repair.KEYS[2]: value})

    def arguments(self):
        (self.root / '.env').write_text(repair.replace_auth('', self.values), encoding='utf-8')
        return SimpleNamespace(root=self.root, account='expected@example.test', output=self.root / 'private' / 'authorization.json')

    def test_wrong_account_never_exports_authorization(self):
        args = self.arguments()
        with patch('google_auth_oauthlib.flow.InstalledAppFlow'), patch.object(repair, 'account_for', return_value='wrong@example.test'), \
             patch.object(repair, 'private_folder') as directory, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(repair.RepairError):
                repair.authorize(args)
        directory.assert_not_called()
        self.assertFalse(args.output.exists())

    def test_private_directory_failure_never_exports_authorization(self):
        args = self.arguments()
        with patch('google_auth_oauthlib.flow.InstalledAppFlow'), patch.object(repair, 'account_for', return_value=args.account), \
             patch.object(repair, 'private_folder', side_effect=repair.RepairError('Cannot protect folder')), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(repair.RepairError):
                repair.authorize(args)
        self.assertFalse(args.output.exists())

    def test_successful_export_keeps_existing_config_and_does_not_print_tokens(self):
        args = self.arguments()
        original = (self.root / '.env').read_bytes()
        args.output.parent.mkdir()
        output = io.StringIO()
        with patch('google_auth_oauthlib.flow.InstalledAppFlow') as flow, \
             patch.object(repair, 'account_for', return_value=args.account), \
             patch.object(repair, 'private_folder', return_value=args.output.parent), contextlib.redirect_stdout(output):
            flow.from_client_config.return_value.run_local_server.return_value = SimpleNamespace(refresh_token='synthetic-refresh')
            repair.authorize(args)
        self.assertEqual((self.root / '.env').read_bytes(), original)
        self.assertTrue(args.output.exists())
        self.assertNotIn('synthetic-secret', output.getvalue())
        self.assertNotIn('synthetic-refresh', output.getvalue())

    def test_library_errors_never_print_secret_bearing_details(self):
        output = io.StringIO()
        with patch('sys.argv', ['repair', 'authorize', '--root', str(self.root), '--account', 'expected@example.test', '--output', str(self.root / 'authorization.json')]), \
             patch.object(repair, 'authorize', side_effect=RuntimeError('synthetic-secret token response')), contextlib.redirect_stdout(output):
            self.assertEqual(repair.main(), 1)
        self.assertNotIn('synthetic-secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
