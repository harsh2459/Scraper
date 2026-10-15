import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('inspect_contabo', Path(__file__).resolve().parents[2] / 'deploy' / 'inspect_contabo.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ContaboInspectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_config_inspection_never_returns_credentials(self):
        path = self.root / '.env'
        path.write_text('PG_PASSWORD=SYNTHETIC-SECRET\nGOOGLE_DRIVE_REFRESH_TOKEN=SYNTHETIC-TOKEN\n'
                        'CUSTOM_PASSWORD=SYNTHETIC-PRIVATE\n', encoding='utf-8')
        result = audit.configured_keys(path)
        self.assertTrue(result['PG_PASSWORD'])
        self.assertTrue(result['GOOGLE_DRIVE_REFRESH_TOKEN'])
        self.assertNotIn('CUSTOM_PASSWORD', result)
        self.assertNotIn('SYNTHETIC', json.dumps(result))

    def test_nginx_inspection_omits_authorization_and_proxy_secrets(self):
        (self.root / 'site').write_text('server_name example.test;\n'
                  'proxy_set_header Authorization "Bearer SYNTHETIC-TOKEN";\n'
                  'proxy_pass https://user:SYNTHETIC-PASSWORD@127.0.0.1:5010/api/?token=SYNTHETIC-QUERY;\n', encoding='utf-8')
        result = audit.nginx_routes(self.root)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]['value'], 'https://127.0.0.1:5010/api/')
        self.assertNotIn('SYNTHETIC', json.dumps(result))

    def test_metadata_queries_are_read_only_and_do_not_return_customer_fields(self):
        for query in (audit.SCHEMA_QUERY, audit.DRIVE_COUNTS_QUERY):
            self.assertIn('BEGIN READ ONLY;', query)
            self.assertIn('statement_timeout', query)
            for mutation in ('INSERT ', 'UPDATE ', 'DELETE ', 'ALTER ', 'CREATE ', 'DROP '):
                self.assertNotIn(mutation, query)
        self.assertNotIn('password_hash', audit.SCHEMA_QUERY)


if __name__ == '__main__':
    unittest.main()
