from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from portal_formats import parse_notice_table, parse_opposition_record, parse_opposition_rows, portal_url

FIXTURES = Path(__file__).parent / 'fixtures' / 'portal'


class PortalFormatTests(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / name).read_text(encoding='utf-8')

    def test_opposition_discovery_uses_opaque_links(self):
        records = parse_opposition_rows(self.fixture('opposition-discovery.html'))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['opposition_no'], '9000001')
        self.assertEqual(records[0]['portal_type'], 'O')
        self.assertIn('fixture-opposition-token', records[0]['details_url'])

    def test_opposition_record_with_nested_tables_and_blank_decision(self):
        record = parse_opposition_record(self.fixture('opposition-record.html'))
        self.assertEqual(record['opposition_reference'], 'Opposition-9000001')
        self.assertEqual(record['opponent_agent_name'], 'Fixture Agent')
        self.assertEqual(record['proceeding_status'], 'Section 21')
        self.assertIsNone(record['decision'])
        # The outer layout table must not duplicate its nested child rows.
        self.assertEqual(len(record['correspondence']), 1)
        self.assertEqual(record['correspondence'][0]['corres_no'], '8000001')

    def test_opposition_reminders_keep_href_document_reference(self):
        records = parse_notice_table(self.fixture('opposition-reminders.html'))
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]['corres_num'])
        self.assertIn('ShowReminderCorrespondence?CRS_NO=fixture-token', records[0]['view_url'])
        self.assertEqual(records[0]['despatch_date'], '03/10/2026')

    def test_reminders_also_support_onclick(self):
        html = self.fixture('opposition-reminders.html').replace(
            'href="/estatus/RegisteredTM/ShowReminderCorrespondence?CRS_NO=fixture-token"',
            'href="#" onclick="ViewReminderDocument(\'fixture-onclick-token\')"')
        record = parse_notice_table(html)[0]
        self.assertEqual(record['corres_num'], 'fixture-onclick-token')
        self.assertIsNone(record['view_url'])

    def test_multiple_proceedings_keep_separate_links(self):
        html = self.fixture('opposition-discovery.html')
        records = parse_opposition_rows(html + html.replace('9000001', '9000002').replace('fixture-opposition-token', 'fixture-second-token'))
        self.assertEqual([r['opposition_no'] for r in records], ['9000001', '9000002'])

    def test_portal_url_root_and_relative_forms(self):
        expected = 'https://tmrsearch.ipindia.gov.in/estatus/RegisteredTM/ShowCorrespondence?CRS_NO=fixture'
        for value in ['/estatus/RegisteredTM/ShowCorrespondence?CRS_NO=fixture',
                      'estatus/RegisteredTM/ShowCorrespondence?CRS_NO=fixture',
                      'RegisteredTM/ShowCorrespondence?CRS_NO=fixture', expected]:
            self.assertEqual(portal_url(value), expected)

    def test_portal_url_rejects_other_origins_and_executable_links(self):
        for value in ['https://unrelated.invalid/doc', '//unrelated.invalid/doc', 'javascript:alert(1)', '']:
            with self.assertRaises(ValueError):
                portal_url(value)

    def test_absent_opposition_table_is_not_a_proceeding(self):
        self.assertEqual(parse_opposition_rows('<html><p>No proceedings listed</p></html>'), [])

    def test_worker_retains_metadata_and_handles_href_reminder_without_network(self):
        from worker_agent.store import LocalStore
        from worker_agent.scraping import PortalScraper
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(directory)
            job = {'id': str(uuid.uuid4()), 'reservation_token': str(uuid.uuid4()), 'app_no': '9000001', 'generation': 1}
            store.receive([job])
            scraper = PortalScraper(store, None)
            downloaded = []
            def download(link):
                downloaded.append(link)
                return b'<html><p>Synthetic notice</p></html>', 'html'
            def unexpected_download(_token):
                raise AssertionError('The href reminder must not use the onclick-only endpoint.')
            scraper.session = SimpleNamespace(
                search_appno_classified=lambda app_no: ('hit', {'Class': '35', 'Application Date': '01/01/2026', '_view_token': 'fixture'}),
                get_pr_details=lambda *args: '<p>Fixture PR details</p>',
                get_app_details=lambda *args: '<p>Fixture application details</p>',
                get_uploaded_documents=lambda *args: [],
                get_reminders=lambda *args: parse_notice_table(self.fixture('opposition-reminders.html')),
                get_correspondence=lambda *args: [],
                download_reminder_document=unexpected_download,
                download_correspondence_document=download,
                get_trademark_image=lambda: None,
            )
            fake_module = SimpleNamespace(SearchOutcome=SimpleNamespace(HIT='hit'))
            with patch.dict('sys.modules', {'ipindia_scraper': fake_module}):
                result, paths = scraper.scrape(job)
            self.assertEqual(result['completeness'], 'complete')
            notice = next(a for a in result['artifacts'] if a['kind'] == 'reminder')
            self.assertEqual(notice['metadata']['corres_no'], '8000002')
            self.assertEqual(notice['metadata']['despatch_date'], '03/10/2026')
            self.assertTrue(Path(paths[notice['sha256']]).is_file())
            self.assertEqual(len(downloaded), 1)
