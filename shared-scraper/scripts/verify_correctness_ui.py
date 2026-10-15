"""Review new screens in the loopback fixture demo; never contact production."""
from datetime import date
import json
from pathlib import Path
import uuid
from urllib.parse import urlparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from control_plane.config import Settings
from control_plane.repository import Repository
from control_plane.contracts import digest
from compliance_facts import add_period, document_events
from worker_agent.scraping import make_result

runtime = Path(__file__).resolve().parents[2] / '.runtime'
cfg = dict(line.split('=', 1) for line in (runtime / 'fleet-demo.env').read_text().splitlines() if '=' in line)
parsed = urlparse(cfg['CONTROL_DATABASE_URL'])
assert parsed.hostname == '127.0.0.1' and parsed.port == 55439 and parsed.path == '/fleet_dev_control'
assert cfg['WORKER_MODE'] == 'fixture' and cfg['CONTROL_ENV'] == 'development'
settings = Settings(cfg['CONTROL_DATABASE_URL'], Path(cfg['CONTROL_ARTIFACT_DIR']), cfg['CONTROL_ADMIN_TOKEN'], publish_legacy=True)
repo = Repository(settings)
worker = repo.create_worker('correctness-ui-fixture-' + uuid.uuid4().hex[:8], 1)
with repo.transaction() as conn:
    job_id = repo._enqueue(conn, '9000001', 200, 'fixture_ui_review', force=True)
    conn.execute('UPDATE jobs SET target_worker_id=%s WHERE id=%s', (worker['id'], job_id))
job = repo.claim(worker['id'], 1)[0]
content = b'<html><body>Fixture notice: file a counter statement within a period of two months from receipt.</body></html>'
metadata = {'subject': 'Fixture Notice of Opposition', 'corres_no': 'fixture-ui-notice', 'source_key': 'application:correspondence:fixture-ui-notice'}
artifact = {'sha256': digest(content), 'filename': 'fixture-review-notice.html', 'size': len(content), 'kind': 'correspondence', 'metadata': metadata}
body = make_result(job, str(uuid.uuid4()), {'Class': '35', 'Application Date': '01/01/2026', 'Status': 'Fixture only', 'Trade Mark': 'DEVELOPMENT FIXTURE'}, [artifact], [], [], 'complete')
body['documents'] = [{'source_key': metadata['source_key'], 'kind': 'correspondence', 'metadata': metadata, 'sha256': artifact['sha256'],
                      'filename': artifact['filename'], 'download_state': 'downloaded', 'parse_state': 'parsed', 'events': document_events(content, metadata)}]
directory = settings.artifact_dir / worker['id'] / body['result_id']
directory.mkdir(parents=True)
(directory / artifact['sha256']).write_bytes(content)
repo.record_artifact(worker['id'], body['result_id'], artifact['sha256'], len(content))
assert repo.complete(worker['id'], body)['disposition'] == 'accepted'
repo.update_worker(worker['id'], {'status': 'retired', 'capacity': 1})

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    context.add_init_script('sessionStorage.setItem("fleet-control-credential", ' + json.dumps(cfg['CONTROL_ADMIN_TOKEN']) + ');'
                            'sessionStorage.setItem("worker-local-credential", ' + json.dumps(cfg['WORKER_LOCAL_TOKEN']) + ');')
    assert context.request.post('http://127.0.0.1:5185/api/login', data={'email': cfg['DEMO_ADMIN_EMAIL'], 'password': cfg['DEMO_ADMIN_PASSWORD']}).status == 200
    # Reset only this synthetic demo notice through the authenticated user API
    # so repeated browser reviews start with an unknown receipt date.
    feed = context.request.get('http://127.0.0.1:5185/api/tasks-compliances?app_no=9000001').json()
    event = next(item for item in feed['event_page']['items'] if item['source_key'] == metadata['source_key'])
    assert context.request.patch('http://127.0.0.1:5185/api/compliance-events/' + str(event['source_id']),
                                 data={'receipt_date': None, 'confirmed_due_date': None, 'status': 'pending'}).status == 200
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:5185/admin/')
    page.locator('.ops-stat').first.wait_for()
    page.get_by_role('heading', name='Fleet connectivity', exact=True).wait_for()
    page.get_by_role('button', name='Fleet machines', exact=True).click()
    page.get_by_role('columnheader', name='Network', exact=True).wait_for()
    page.get_by_text('Fixture simulation', exact=True).first.wait_for()
    page.screenshot(path=str(runtime / 'network-admin.png'), full_page=True)
    for title, path in [('Document coverage', 'document_ledger'), ('Opposition records', 'proceedings'), ('Compliance facts', 'compliance_events')]:
        with page.expect_response(lambda reply: '/api/control/v1/admin/' + path in reply.url) as response:
            page.get_by_role('button', name=title, exact=True).click()
        assert response.value.status == 200, title
        assert not page.locator('[role="alert"]').count(), title
    page.screenshot(path=str(runtime / 'correctness-admin.png'), full_page=True)
    page.goto('http://127.0.0.1:5185/tasks')
    card = page.locator('div.rounded-lg').filter(has_text='Counter Statement').filter(has_text='#9000001').first
    card.get_by_role('button', name='Review / update', exact=True).wait_for()
    card.get_by_role('button', name='Review / update', exact=True).click()
    card.get_by_label('Receipt date', exact=True).fill(date.today().isoformat())
    card.get_by_role('button', name='Save review', exact=True).click()
    # The calculated date is outside the current month; move into its month.
    due = add_period(date.today(), 2, 'months')
    for _ in range(2):
        page.locator('button').filter(has=page.locator('.lucide-chevron-right')).first.click()
    card = page.locator('div.rounded-lg').filter(has_text='Counter Statement').filter(has_text='#9000001').first
    card.get_by_role('button', name='Review / update', exact=True).wait_for()
    card.get_by_role('button', name='Review / update', exact=True).click()
    assert card.get_by_label('Deadline', exact=True).input_value() == due.isoformat()
    page.screenshot(path=str(runtime / 'correctness-receipt-review.png'), full_page=True)
    card.get_by_label('I checked the source and confirm the deadline above.').check()
    card.get_by_role('button', name='Save review', exact=True).click()
    page.get_by_role('button', name='List', exact=True).click()
    page.get_by_text('Confirmed by you', exact=False).wait_for()
    page.screenshot(path=str(runtime / 'correctness-confirmed.png'), full_page=True)
    page.goto('http://127.0.0.1:5011/')
    page.get_by_role('heading', name='Network connectivity', exact=True).wait_for()
    page.get_by_text('Fixture simulation. Live VPN connectivity has not been verified.', exact=True).wait_for()
    page.screenshot(path=str(runtime / 'network-worker.png'), full_page=True)
    page.get_by_role('button', name='Assignments & results', exact=True).click()
    page.locator('.ops-table-card').wait_for()
    assert not errors, errors
    context.close()
    browser.close()
print('Browser review passed: fleet/worker connectivity, ledger/proceeding/event pages, receipt calculation, source confirmation, worker results.')
