"""Subprocess assertions, invoked only by the isolated integration suite."""
import os
from pathlib import Path
import sys
import tempfile

root = Path(__file__).parent.parent
sys.path.insert(0, str(root / 'tm_manager'))
sys.path.insert(1, str(root))

from migrations.prepare_legacy import prepare
prepare(os.environ['TM_DATABASE_URL'])  # validates localhost test/dev restriction
import app as user_app
import db
from control_plane.app import create_app
from control_plane.config import Settings
from control_plane.repository import Repository
from control_plane.contracts import digest
from worker_agent.scraping import make_result
from compliance_facts import events_from_text
from werkzeug.security import generate_password_hash
import uuid

conn = db.get_connection()
try:
    # Real user ownership and subscription tables, synthetic identities only.
    conn.execute("DELETE FROM my_trademarks WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'fixture-%@example.invalid')")
    conn.execute("DELETE FROM users WHERE email LIKE 'fixture-%@example.invalid'")
    ids = []
    for n in (1, 2):
        row = conn.execute('INSERT INTO users(email,password_hash,is_admin,is_active) VALUES(%s,%s,false,true) RETURNING id',
                           (f'fixture-{n}@example.invalid', generate_password_hash('fixture-password'))).fetchone()
        ids.append(row['id'])
    conn.commit()
finally:
    conn.close()

client = user_app.app.test_client()
assert client.post('/api/login', json={'email': 'fixture-1@example.invalid', 'password': 'fixture-password'}).status_code == 200
response = client.post('/api/add', json={'app_no': '7654321'})
assert response.status_code == 200, response.json
conn = db.get_connection()
try:
    row = conn.execute('SELECT * FROM my_trademarks WHERE user_id=%s AND app_no=%s', (ids[0], '7654321')).fetchone()
    assert row['status'] == 'pending' and row['job_id'] is None
    count = conn.execute('SELECT count(*) AS n FROM public.fleet_fetch_requests WHERE app_no=%s AND consumed_at IS NULL', ('7654321',)).fetchone()['n']
    assert count == 1
finally:
    conn.close()
settings = Settings(os.environ['TM_DATABASE_URL'], Path(os.environ['CONTROL_ARTIFACT_DIR']), 'synthetic-control-admin-token', publish_legacy=True)
repo = Repository(settings)
repo.dispatch()
worker = repo.create_worker('user-handoff-worker')
job = repo.claim(worker['id'], 1)[0]
result_id = str(uuid.uuid4())
content = b'fixture document for portfolio delivery'
artifact = {'sha256': digest(content), 'filename': 'fixture-document.txt', 'size': len(content), 'kind': 'document'}
directory = settings.artifact_dir / worker['id'] / result_id
directory.mkdir(parents=True)
(directory / artifact['sha256']).write_bytes(content)
repo.record_artifact(worker['id'], result_id, artifact['sha256'], len(content))
result = make_result(job, result_id, {'Class': '35', 'Application Date': '01/01/2026', 'Status': 'Registered', 'Trade Mark': 'FIXTURE'}, [artifact], [], [], 'complete')
result['documents'] = [{'source_key': 'application:document:fixture-notice', 'kind': 'document', 'metadata': {'subject':'Notice of Opposition'},
                       'sha256': artifact['sha256'], 'filename': artifact['filename'], 'download_state': 'downloaded', 'parse_state': 'parsed',
                       'events': events_from_text('Counter statement within two months from receipt.', {'subject': 'Notice of Opposition'})}]
assert repo.complete(worker['id'], result)['disposition'] == 'accepted'
detail = client.get(f"/api/trademark/{row['id']}")
assert detail.status_code == 200, detail.json
assert detail.json['tm']['status'] == 'fetched'
assert detail.json['data']['status'] == 'Registered'
assert detail.json['tm']['fleet_schedule']['next_fetch_at']
assert detail.json['tm']['fleet_job']['state'] == 'completed'
assert 'fixture-document.txt' in detail.json['documents']
document = client.get(f"/trademark/{row['id']}/document/fixture-document.txt")
assert document.status_code == 200 and document.data == content
document.close()
feed = client.get('/api/tasks-compliances?app_no=7654321')
assert feed.status_code == 200, feed.json
assert feed.json['event_page']['total'] == 1
event = feed.json['event_page']['items'][0]
assert event['due_date'] is None and event['needs_review']
eid = event['source_id']
proof = client.get(f'/api/compliance-events/{eid}/document')
assert proof.status_code == 200 and proof.data == content
assert 'sandbox' in proof.headers['Content-Security-Policy']
proof.close()
review = client.patch(f'/api/compliance-events/{eid}', json={'receipt_date': '2026-08-31', 'confirmed_due_date':'2026-10-31', 'status':'completed'})
assert review.status_code == 200, review.json
confirmed = client.get('/api/tasks-compliances?app_no=7654321').json['event_page']['items'][0]
assert confirmed['due_date'] == '2026-10-31' and confirmed['receipt_date'] == '2026-08-31' and not confirmed['needs_review']
assert client.post(f"/api/trademark/{row['id']}/live-refresh", json={}).status_code == 202
assert client.get('/api/tm/leads').status_code == 403
assert client.get('/api/control/v1/admin/overview').status_code == 403

# Existing session authentication is the administrator relay's boundary.
from unittest.mock import MagicMock, patch
conn = db.get_connection()
try:
    admin_id = conn.execute("INSERT INTO users(email,password_hash,is_admin,is_active) VALUES('fixture-admin@example.invalid',%s,true,true) RETURNING id",
                            (generate_password_hash('fixture-admin-password'),)).fetchone()['id']
    conn.commit()
finally:
    conn.close()
admin_client = user_app.app.test_client()
assert admin_client.post('/api/login', json={'email': 'fixture-admin@example.invalid', 'password': 'fixture-admin-password'}).status_code == 200
reply = MagicMock(status_code=200, headers={'Content-Type': 'application/json'})
reply.iter_content.return_value = [b'{"queued":0}']
with patch.dict(os.environ, {'TM_CONTROL_API_BASE': 'http://127.0.0.1:5510', 'TM_CONTROL_ADMIN_TOKEN': 'synthetic-server-token'}), patch('control_admin_proxy.requests.Session') as network:
    network.return_value.__enter__.return_value.request.return_value.__enter__.return_value = reply
    assert admin_client.get('/api/control/v1/admin/overview').json == {'queued': 0}
    assert admin_client.post('/api/control/v1/admin/jobs', json={}).status_code == 403
    conn = db.get_connection()
    try:
        conn.execute('UPDATE users SET is_active=false WHERE id=%s', (admin_id,))
        conn.commit()
    finally:
        conn.close()
    assert admin_client.get('/api/control/v1/admin/overview').status_code == 401

# A second user cannot read the first user's private portfolio or documents.
second = user_app.app.test_client()
assert second.post('/api/login', json={'email': 'fixture-2@example.invalid', 'password': 'fixture-password'}).status_code == 200
assert second.get(f"/api/trademark/{row['id']}").status_code == 404
assert second.get(f"/trademark/{row['id']}/document/fixture-document.txt").status_code == 404
assert second.get(f'/api/compliance-events/{eid}/document').status_code == 404
assert second.patch(f'/api/compliance-events/{eid}', json={'status':'completed'}).status_code == 404
assert second.post('/api/add', json={'app_no': '7654321'}).status_code == 200
other_event = second.get('/api/tasks-compliances?app_no=7654321').json['event_page']['items'][0]
assert other_event['due_date'] is None and other_event['status'] == 'pending' and other_event['receipt_date'] is None
repo.dispatch()  # current open manual refresh deduplicates the shared application
assert repo.list('jobs')['total'] == 2
# Old fleet calls cannot mutate a competing queue in new mode.
assert client.post('/api/fleet/register', json={'machine_key': 'legacy-fixture'}).status_code == 410
assert client.get('/api/fleet-status').status_code == 410
print('User handoff, accepted projection, documents, refresh queue and ownership passed.')
