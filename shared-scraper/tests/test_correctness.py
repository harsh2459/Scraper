"""Offline regression coverage for document/event correctness and recovery."""
from dataclasses import replace
from datetime import date
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from compliance_facts import add_period, events_from_text
from control_plane.contracts import digest, validate_result, utcnow
from control_plane.repository import Conflict, Repository
from document_identity import metadata_fingerprint
from pdf_scraper.text_fields import application_fields, labeled_block, merge_fields, pdf_text
from portal_formats import parse_notice_table, require_list_fragment
from tm_manager.fleet_compliance import list_events, update_event
from worker_agent.scraping import PortalScraper, make_result
from worker_agent.store import LocalStore
from tests import test_fleet as fleet
from migrations.prepare_legacy import prepare

FIXTURES = Path(__file__).parent / 'fixtures' / 'portal'


def text_pdf(lines):
    # A real synthetic text PDF, independent of optional PDF-writer packages.
    stream = 'BT /F1 10 Tf 12 TL 40 760 Td ' + ' '.join(
        '(' + line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ') Tj T*' for line in lines) + ' ET'
    content = stream.encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    output = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
    xref = len(output)
    output.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    return bytes(output)


class ExtractionTests(unittest.TestCase):
    def test_forward_reversed_and_multiline_label_values(self):
        forward = labeled_block('Name\nFixture Owner\nAddress\nFirst line\nSecond line\nMobile No.\nEmail Address\nfixture@example.invalid', ['Name','Address','Mobile No.','Email Address'])
        self.assertEqual(forward['Name'], 'Fixture Owner')
        self.assertEqual(forward['Address'], 'First line Second line')
        self.assertEqual(forward['Mobile No.'], '')
        reversed_ = labeled_block('Fixture Owner\nName\nFirst line\nSecond line\nAddress\nMobile No.\nfixture@example.invalid\nEmail Address', ['Name','Address','Mobile No.','Email Address'])
        self.assertEqual(reversed_, forward)
    def test_real_pdf_independent_class_and_use_without_old_suffixes(self):
        text = pdf_text(text_pdf(['FORM TM-A', 'CLASS OF GOODS OR SERVICE', 'Description: Consulting services',
                                 'Class: 35', 'STATEMENT AS TO USE OF MARK', 'Proposed to be used']))
        fields = application_fields(text)
        self.assertEqual(fields['class']['number'], '35')
        self.assertIn('Consulting', fields['class']['description'])
        self.assertEqual(fields['use_statement'], 'Proposed to be used')

    def test_multiple_classes_and_explicit_blank_are_not_missing(self):
        fields = application_fields('FORM TM-A\nMARK DETAILS\nCategory of Mark: DEVICE\nTrade Mark:\nImage Description: Circle\n'
                                    'CLASS OF GOODS OR SERVICES\nClass: 9 Description: Software\nClass: 42 Description: Hosting\n'
                                    'USE OF MARK: Used since 01/01/2020\nVERIFICATION')
        self.assertEqual(fields['class']['number'], '9, 42')
        self.assertIsNone(fields['mark']['trade_mark'])
        self.assertEqual(fields['mark']['image_description'], 'Circle')
        merged = merge_fields({'agent': {'name': 'Known Agent', 'address': None}, 'mark': {'trade_mark': 'Old'}}, fields)
        self.assertEqual(merged['agent']['name'], 'Known Agent')
        self.assertNotIn('address', merged['agent'])
        self.assertIsNone(merged['mark']['trade_mark'])

    def test_reversed_use_label_does_not_clear_the_actual_statement(self):
        fields = application_fields('FORM TM-A\nCLASS OF GOODS OR SERVICE\nDescription: Consulting\nClass: 35\n'
                                    'Proposed to be used\nSTATEMENT AS TO\nUSE OF MARK\nANY OTHER IMPORTANT INFORMATION')
        self.assertEqual(fields['use_statement'], 'Proposed to be used')
        self.assertNotIn('Proposed', fields['class']['description'])

    def test_configured_ocr_is_bounded_and_reports_its_origin(self):
        from PIL import Image
        from types import SimpleNamespace
        image = Image.new('RGB', (20, 20), 'white')
        page = SimpleNamespace(extract_text=lambda: '', to_image=lambda **_: SimpleNamespace(original=image))
        class PDF:
            pages = [page]
            def __enter__(self): return self
            def __exit__(self, *_): pass
        with patch.dict('os.environ', {'WORKER_OCR_EXECUTABLE': 'fixture-tesseract'}), \
             patch('pdf_scraper.text_fields.pdfplumber.open', return_value=PDF()), \
             patch('pdf_scraper.text_fields.subprocess.run', return_value=SimpleNamespace(stdout=b'FORM TM-A\nClass: 35')) as run:
            text, used_ocr = pdf_text(b'fixture scan', with_info=True)
            self.assertTrue(used_ocr)
            self.assertIn('Class: 35', text)
            self.assertEqual(run.call_args.args[0][0], 'fixture-tesseract')
            self.assertEqual(run.call_args.kwargs['timeout'], 60)

    def test_calendar_months_and_missing_receipt(self):
        self.assertEqual(add_period(date(2026, 12, 31), 2, 'months'), date(2027, 2, 28))
        event = events_from_text('File a counter statement within two months from the date of receipt.',
                                 {'subject': 'Notice of Opposition', 'corres_date': '30/08/2026'})[0]
        self.assertIsNone(event['due_date'])
        self.assertEqual((event['period_value'], event['period_unit'], event['anchor']), (2, 'months', 'receipt_date'))
        self.assertTrue(event['needs_review'])

    def test_saved_notice_period_wording_uses_receipt_anchor(self):
        event = events_from_text((FIXTURES / 'opposition-period.txt').read_text(), {'subject': 'Notice of Opposition'})[0]
        self.assertEqual((event['period_value'], event['period_unit'], event['anchor']), (2, 'months', 'receipt_date'))
        self.assertIsNone(event['due_date'])

    def test_acceptance_body_overrides_hearing_title(self):
        event = events_from_text('Your application has been accepted for advertisement.', {'subject': 'Hearing Notice'})[0]
        self.assertEqual(event['event_type'], 'application_accepted')
        self.assertIsNone(event['due_date'])

    def test_hearing_uses_body_date_not_letter_date(self):
        metadata = {'subject': 'Hearing notice', 'corres_date': '01/10/2026'}
        self.assertIsNone(events_from_text('Please review this notice.', metadata)[0]['due_date'])
        event = events_from_text('The hearing is fixed on 21/11/2026.', metadata)[0]
        self.assertEqual(event['due_date'], '2026-11-21')
        self.assertFalse(event['needs_review'])

    def test_error_list_is_not_an_empty_success(self):
        for html in ('', '<p>Session expired</p>', '<p>Unexpected response</p>', '<input type="password">'):
            with self.assertRaises(ValueError):
                require_list_fragment(html)
        require_list_fragment('<p>No records found</p>')
        require_list_fragment((FIXTURES / 'opposition-reminders.html').read_text())


class WorkerCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(self.temp.name)
        self.scraper = PortalScraper(self.store, None)
        self.downloads = []
        self.rows = [{'corres_no': '8000001', 'corres_date': '01/10/2026', 'subject': 'Hearing Notice', 'view_url': '/notice-one'}]
        self.details = '<p>Application details. Opposition/Rectification: No records found</p>'
        self.session = SimpleNamespace(
            search_appno_classified=lambda _: ('hit', {'Class': '35', 'Application Date': '01/01/2026', 'Status': 'Opposed', '_view_token': 'fresh'}),
            get_app_details=lambda *_: self.details, get_pr_details=lambda *_: '<p>Proprietor details</p>',
            get_uploaded_documents=lambda *_: [], get_reminders=lambda *_: self.rows, get_correspondence=lambda *_: [],
            download_correspondence_document=self.download, get_trademark_image=lambda: None)
        self.scraper.session = self.session

    def job(self, **kwargs):
        job = {'id': str(uuid.uuid4()), 'reservation_token': str(uuid.uuid4()), 'generation': 1, 'app_no': '9000001', **kwargs}
        self.store.receive([job])
        return job

    def scrape(self, job=None):
        with patch.dict('sys.modules', {'ipindia_scraper': SimpleNamespace(SearchOutcome=SimpleNamespace(HIT='hit'))}):
            return self.scraper.scrape(job or self.job())

    def download(self, url):
        self.downloads.append(url)
        return b'<html>Your application has been accepted for advertisement.</html>', 'html'

    def test_repeat_refresh_reuses_bytes_but_new_reference_is_fetched(self):
        first, _ = self.scrape()
        self.rows[0]['view_url'] = '/rotated-session-token'
        second, _ = self.scrape()
        self.assertEqual(len(self.downloads), 1)
        self.assertTrue(second['documents'][0]['reused'])
        self.rows.append({**self.rows[0], 'corres_no': '8000002', 'view_url': '/notice-two'})
        third, _ = self.scrape()
        self.assertEqual(len(self.downloads), 2)
        self.assertEqual(len(third['documents']), 2)
        # Different notices with identical bytes keep both identities.
        self.assertEqual(len({d['source_key'] for d in third['documents']}), 2)
        validate_result(first)
        validate_result(second)
        validate_result(third)

    def test_metadata_change_force_full_and_checksum_damage_refetch(self):
        first, paths = self.scrape()
        self.rows[0]['subject'] = 'Updated subject'
        self.scrape()
        self.assertEqual(len(self.downloads), 2)
        self.scrape(self.job(force_full=True))
        self.assertEqual(len(self.downloads), 3)
        for path in paths.values():
            if Path(path).name == first['documents'][0]['sha256']:
                Path(path).write_bytes(b'damaged')
        # Corrupt the current cache's referenced file, not just an older copy.
        with self.store.connect() as conn:
            path = conn.execute('SELECT path FROM document_cache').fetchone()['path']
        Path(path).write_bytes(b'damaged')
        self.scrape()
        self.assertEqual(len(self.downloads), 4)

    def test_missing_download_retries_without_redownloading_good_document(self):
        self.rows.append({**self.rows[0], 'corres_no': '8000002', 'view_url': '/failed'})
        original = self.session.download_correspondence_document
        self.session.download_correspondence_document = lambda url: None if url == '/failed' else original(url)
        first, _ = self.scrape()
        self.assertEqual(first['completeness'], 'partial')
        self.session.download_correspondence_document = original
        second, _ = self.scrape()
        self.assertEqual(second['completeness'], 'complete')
        self.assertEqual(self.downloads, ['/notice-one', '/failed'])

    def test_full_safety_interval_expires_cache(self):
        self.scrape()
        with self.store.connect() as conn:
            conn.execute('UPDATE document_cache SET checked_at=0')
        self.scrape()
        self.assertEqual(len(self.downloads), 2)

    def test_opposition_fetch_preserves_namespace_and_record(self):
        self.details = (FIXTURES / 'opposition-discovery.html').read_text()
        self.rows = []
        def page(url):
            name = 'opposition-record.html' if 'ShowOppRecDetails' in url else 'opposition-reminders.html'
            return (FIXTURES / name).read_text()
        self.session.get_proceeding_page = page
        result, _ = self.scrape()
        self.assertEqual(result['completeness'], 'complete')
        self.assertEqual(result['proceedings'][0]['record']['opposition_reference'], 'Opposition-9000001')
        self.assertEqual({d['kind'] for d in result['documents']}, {'opposition_correspondence', 'opposition_reminder'})
        self.assertTrue(all(d['source_key'].startswith('O:9000001:') for d in result['documents']))
        validate_result(result)

    def test_optional_logo_failure_does_not_block_valid_data(self):
        def fail():
            raise ConnectionError('Synthetic logo failure')
        self.session.get_trademark_image = fail
        result, _ = self.scrape()
        self.assertEqual(result['completeness'], 'complete')
        self.assertTrue(result['warnings'])
        validate_result(result)


class PublicationCorrectnessTests(unittest.TestCase):
    setUpClass = classmethod(fleet.FleetTests.setUpClass.__func__)
    body = fleet.FleetTests.body
    reserve = fleet.FleetTests.reserve

    def setUp(self):
        fleet.FleetTests.setUp(self)
        prepare(self.url)
        with self.repo.transaction() as conn:
            conn.execute("DELETE FROM public.tma_documents WHERE app_no='9900011'")

    def publish(self, fields=None, events=None, partial=False):
        prepare(self.url)
        repo = Repository(replace(self.settings, publish_legacy=True))
        job = self.reserve('9900011')
        content = b'Synthetic saved artifact ' + repr(fields).encode()
        sha = digest(content)
        artifact = {'sha256': sha, 'filename': 'synthetic.pdf', 'size': len(content), 'kind': 'document'}
        body = self.body(job, [artifact])
        body['documents'] = [{'source_key': 'application:document:fixture-one', 'kind': 'document', 'metadata': {'description': 'TM-A', 'date': '01/01/2026'},
                              'sha256': sha, 'filename': 'synthetic.pdf', 'download_state': 'downloaded', 'parse_state': 'parsed', 'fields': fields, 'events': events or []}]
        if fields:
            body['extracted'] = [{'sha256': sha, 'fields': fields}]
        if partial:
            body.update(completeness='partial', warnings=['Missing unrelated document'], stages={'core': {'state': 'complete', 'required': True}, 'other': {'state': 'failed', 'required': True}})
        path = self.settings.artifact_dir / self.worker['id'] / body['result_id'] / sha
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        repo.record_artifact(self.worker['id'], body['result_id'], sha, len(content))
        reply = repo.complete(self.worker['id'], body)
        return repo, body, reply

    def test_content_dedup_and_merge_preserves_fields_and_explicit_blank(self):
        self.publish({'form_type': 'TM-A', 'applicant': {'name': 'Fixture Owner'}, 'class': {'number': '35'}, 'use_statement': 'Proposed'})
        self.publish({'form_type': 'TM-A', 'applicant': {'name': 'Fixture Owner'}, 'class': {'number': '35'}, 'use_statement': 'Proposed'})
        with self.repo.transaction() as conn:
            rows = conn.execute("SELECT * FROM public.tma_documents WHERE app_no='9900011' AND fleet_sha256 IS NOT NULL").fetchall()
            self.assertEqual(len(rows), 1)
        self.publish({'form_type': 'TM-A', 'use_statement': None})
        with self.repo.transaction() as conn:
            canonical = conn.execute("SELECT * FROM public.tma_documents WHERE app_no='9900011' AND fleet_canonical").fetchone()
            self.assertEqual(canonical['applicant_name'], 'Fixture Owner')
            self.assertEqual(canonical['class_number'], '35')
            self.assertIsNone(canonical['use_statement'])

    def test_stage_partial_publishes_core_without_advancing_schedule(self):
        repo, body, reply = self.publish({'class': {'number': '35'}}, partial=True)
        self.assertEqual(reply['disposition'], 'partial')
        with repo.transaction() as conn:
            tm = conn.execute("SELECT * FROM trademarks WHERE app_no='9900011'").fetchone()
            search = conn.execute('SELECT * FROM public.ipindia_results WHERE query=%s ORDER BY id DESC LIMIT 1', ('9900011',)).fetchone()
            self.assertIsNotNone(search)
            self.assertIsNotNone(tm['last_published_at'])
            self.assertIsNone(tm['last_successful_fetch_at'])
            self.assertEqual(conn.execute('SELECT state FROM jobs WHERE id=%s', (body['job_id'],)).fetchone()['state'], 'retry_wait')

    def test_event_identity_receipt_confirmations_side_and_pagination(self):
        event = events_from_text('Counter statement within two months from receipt.', {'subject': 'Notice of Opposition'})[0]
        repo, _, _ = self.publish(events=[event])
        with repo.transaction() as conn:
            uid = conn.execute("INSERT INTO public.users(email,password_hash) VALUES(%s,'synthetic') RETURNING id", ('correctness-' + str(uuid.uuid4()) + '@example.invalid',)).fetchone()['id']
            other = uid + 1000000
            conn.execute("INSERT INTO public.my_trademarks(app_no,user_id,kind) VALUES('9900011',%s,'portfolio')", (uid,))
            eid = conn.execute("SELECT id FROM compliance_events WHERE app_no='9900011'").fetchone()['id']
        self.addCleanup(self.remove_user, uid)
        with repo.transaction() as conn:
            self.assertFalse(update_event(conn, eid, other, {'status': 'completed'}))
            self.assertTrue(update_event(conn, eid, uid, {'receipt_date': '2026-08-31', 'status': 'completed'}))
            page = list_events(conn, uid, date(2026, 10, 1), date(2026, 10, 31), limit=1)
            self.assertEqual(page['total'], 1)
            self.assertEqual(page['items'][0]['due_date'], '2026-10-31')
            self.assertTrue(page['items'][0]['needs_review'])
            update_event(conn, eid, uid, {'confirmed_due_date': '2026-10-31'})
        repo, _, _ = self.publish(events=[event])
        with repo.transaction() as conn:
            page = list_events(conn, uid, date(2026, 10, 1), date(2026, 10, 31))
            self.assertEqual(page['items'][0]['source_id'], eid)
            self.assertEqual(page['items'][0]['status'], 'completed')
            self.assertFalse(page['items'][0]['needs_review'])
            self.assertEqual(list_events(conn, other, date(2020, 1, 1), date(2030, 1, 1))['total'], 0)
        changed = events_from_text('Counter statement within three months from receipt.', {'subject': 'Notice of Opposition'})[0]
        repo, _, _ = self.publish(events=[changed])
        with repo.transaction() as conn:
            revised = list_events(conn, uid, date(2026, 10, 1), date(2026, 12, 31))['items'][0]
            self.assertEqual(revised['due_date'], '2026-11-30')
            self.assertEqual(revised['due_basis'], 'confirmation_stale')
            self.assertTrue(revised['needs_review'])
            self.assertEqual(revised['status'], 'completed')

    def test_large_portfolio_date_pagination_and_explicit_outgoing_membership(self):
        repo, _, _ = self.publish()
        with repo.transaction() as conn:
            uid = conn.execute("INSERT INTO public.users(email,password_hash) VALUES(%s,'synthetic') RETURNING id", ('large-' + str(uuid.uuid4()) + '@example.invalid',)).fetchone()['id']
            conn.execute("INSERT INTO public.my_trademarks(app_no,user_id,kind) VALUES('9900011',%s,'portfolio'),('9900011',%s,'opposition_out')", (uid, uid))
            conn.execute("INSERT INTO compliance_events(app_no,source_key,event_type,category,due_date,due_basis,needs_review,facts,observed_at) "
                         "SELECT '9900011','pagination:' || n,'hearing_scheduled','hearing_show_cause','2026-10-03','stated',false,'{}',now() FROM generate_series(1,1205) n")
            conn.execute("INSERT INTO compliance_events(app_no,source_key,event_type,category,proceeding_key,opposition_no,applies_to_side,due_basis,facts,observed_at) "
                         "VALUES('9900011','O:9000001:evidence','evidence_due','evidence_rule_45','O:9000001','9000001','opposition_out','unknown','{}',now())")
        self.addCleanup(self.remove_user, uid)
        with repo.transaction() as conn:
            page = list_events(conn, uid, date(2026, 10, 1), date(2026, 10, 31), limit=200, offset=1000)
            self.assertEqual(page['total'], 1205)
            self.assertEqual(len(page['items']), 200)
            self.assertEqual(list_events(conn, uid, date(2026, 10, 1), date(2026, 10, 31), side='opposition_out')['total'], 0)
            conn.execute("INSERT INTO public.fleet_proceeding_memberships(user_id,app_no,proceeding_key) VALUES(%s,'9900011','O:9000001')", (uid,))
            page = list_events(conn, uid, date(2026, 10, 1), date(2026, 10, 31), side='opposition_out')
            self.assertEqual(page['total'], 1)
            self.assertEqual(page['items'][0]['opposition_no'], '9000001')

    def remove_user(self, uid):
        with self.repo.transaction() as conn:
            conn.execute('DELETE FROM public.my_trademarks WHERE user_id=%s', (uid,))
            conn.execute('DELETE FROM public.users WHERE id=%s', (uid,))

    def test_repair_is_bound_to_original_worker_and_keeps_fetch_schedule(self):
        repo, original, _ = self.publish({'class': {'number': '35'}})
        before = repo.list('trademarks')['items'][0]
        second = repo.create_worker('repair-cannot-move')
        with self.assertRaises(Conflict):
            repo.request_extraction_repair(second['id'], original['result_id'])
        repo.request_extraction_repair(self.worker['id'], original['result_id'])
        self.assertEqual(repo.claim(second['id'], 1), [])
        job = repo.claim(self.worker['id'], 1)[0]
        repaired = self.body(job)
        repaired.update(repair_only=True, portal_observed_at=original['observed_at'])
        self.assertEqual(repo.complete(self.worker['id'], repaired)['disposition'], 'accepted')
        after = repo.list('trademarks')['items'][0]
        self.assertEqual(before['last_successful_fetch_at'], after['last_successful_fetch_at'])
        self.assertEqual(before['next_fetch_at'], after['next_fetch_at'])

    def test_local_repair_cycle_never_connects_vpn_or_calls_scraper(self):
        engine, client = fleet.FleetTests.engine(self)
        self.repo.enqueue('9900011')
        self.assertTrue(engine.cycle(), engine.last_error)
        original = engine.store.list(('acknowledged',))[0]['result']
        self.repo.request_extraction_repair(self.worker['id'], original['result_id'])
        self.repo.enqueue('9900012')
        with patch.object(engine.vpn, 'connect', side_effect=AssertionError('Repair must not connect VPN')), \
             patch.object(engine.network, 'preflight', side_effect=AssertionError('Broken VPN must not block saved PDF repair')), \
             patch.object(engine.scraper, 'scrape', side_effect=AssertionError('Repair must not scrape portal')):
            self.assertTrue(engine.cycle(), engine.last_error)
        repaired = next(item['result'] for item in engine.store.list(('acknowledged',)) if item['result']['result_id'] != original['result_id'])
        self.assertTrue(repaired['repair_only'])
        self.assertEqual(repaired['portal_observed_at'], original['observed_at'])
        with self.repo.transaction() as conn:
            queued = conn.execute("SELECT state,attempts FROM jobs WHERE app_no='9900012'").fetchone()
            self.assertEqual(queued['state'], 'queued')
            self.assertEqual(queued['attempts'], 0)


if __name__ == '__main__':
    unittest.main()
