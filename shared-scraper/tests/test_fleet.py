import concurrent.futures
from dataclasses import replace
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
from urllib.parse import urlparse

import psycopg

from control_plane.app import create_app
from control_plane.config import Settings
from control_plane.contracts import digest, utcnow
from control_plane.repository import Conflict, Repository
from migrations.__main__ import migrate
from worker_agent.config import WorkerSettings
from worker_agent.engine import FixtureVPN, WorkerEngine
from worker_agent.scraping import make_result
from worker_agent.store import LocalStore


class FlaskTransport:
    def __init__(self, app, token, vpn):
        self.client = app.test_client()
        self.token = token
        self.vpn = vpn
        self.fail_completion = False
        self.lose_ack = False
        self.allow_vpn = False
        self.vpn_calls = []
        self.fail_online = False

    def call(self, method, path, **kwargs):
        if self.vpn.active:
            if not self.allow_vpn:
                raise AssertionError('Coordinator contacted while VPN active')
            self.vpn_calls.append(path)
            if self.fail_online:
                raise ConnectionError('Simulated coordinator outage during VPN')
        response = self.client.open('/api/control/v1/worker' + path, method=method,
                                    headers={'Authorization': 'Bearer ' + self.token, **kwargs.pop('headers', {})}, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(response.get_json())
        return response.get_json()

    def heartbeat(self, phase, metadata):
        return self.call('POST', '/heartbeat', json={'phase': phase, 'metadata': metadata})

    def health(self):
        if self.vpn.active and not self.allow_vpn:
            raise AssertionError('Health checked while offline VPN active')
        if self.vpn.active and self.fail_online:
            raise ConnectionError('Simulated health outage')
        response = self.client.get('/api/control/v1/health')
        if response.status_code != 200 or response.get_json()['status'] != 'ok':
            raise RuntimeError('Health failed')

    def reconcile(self):
        return self.call('GET', '/reconcile')['assignments']

    def claim(self, limit, repair_only=False):
        return self.call('POST', '/claim', json={'limit': limit, 'repair_only': repair_only})['assignments']

    def offline(self, jobs):
        return self.call('POST', '/offline', json={'assignments': jobs})

    def upload(self, result, artifact, content):
        return self.call('PUT', f"/artifacts/{result['result_id']}/{artifact['sha256']}", data=content,
                         headers={'X-Job-ID': result['job_id'], 'X-Reservation-Token': result['reservation_token']})

    def complete(self, result):
        if self.fail_completion:
            raise ConnectionError('Simulated outage')
        reply = self.call('POST', '/complete', json=result)
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError('Simulated lost acknowledgment')
        return reply

    def fail(self, job, error):
        return self.call('POST', '/fail', json={'job_id': job['id'], 'reservation_token': job['reservation_token'], 'error': error})


class FleetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('FLEET_TEST_DATABASE_URL', 'postgresql://fleet_test_admin@127.0.0.1:55439/fleet_test_control')
        parsed = urlparse(cls.url)
        if parsed.hostname != '127.0.0.1' or not parsed.path.startswith('/fleet_test'):
            raise RuntimeError('Tests require an explicit localhost fleet_test database.')
        migrate(cls.url)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.url, self.root / 'server', 'test-admin-credential-for-isolated-db')
        self.repo = Repository(self.settings)
        with self.repo.transaction() as conn:
            conn.execute('TRUNCATE workers,trademarks,jobs,attempts,results,artifacts,audit,accounts,enrollment_keys,public.fleet_fetch_requests RESTART IDENTITY CASCADE')
            conn.execute("DELETE FROM policies WHERE id NOT IN ('weekly','three_weeks','four_weeks','paused')")
        self.app = create_app(self.settings)
        self.app.testing = True
        self.worker = self.repo.create_worker('test-worker', 5)

    def reserve(self, app_no='1234567', worker=None):
        self.repo.enqueue(app_no)
        return self.repo.claim((worker or self.worker)['id'], 1)[0]

    def body(self, job, artifacts=None):
        return make_result(job, str(uuid.uuid4()), {'Class': '35', 'Application Date': '01/01/2026', 'Status': 'Registered'}, artifacts or [], [], [], 'complete')

    def test_import_and_configuration_never_inherit_legacy_database(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'CONTROL_DATABASE_URL': '', 'PG_HOST': 'production.invalid'}):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_atomic_claim_between_two_workers(self):
        second = self.repo.create_worker('second')
        for number in range(10):
            self.repo.enqueue(str(1000000 + number))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda w: self.repo.claim(w['id'], 5), [self.worker, second]))
        self.assertEqual(len({j['id'] for group in claims for j in group}), 10)

    def test_repeated_claim_returns_same_reservation(self):
        first = self.reserve()
        repeated = self.repo.claim(self.worker['id'], 5)
        self.assertEqual(repeated[0]['reservation_token'], first['reservation_token'])

    def test_bad_local_preflight_never_consumes_portal_attempts(self):
        from unittest.mock import patch
        from worker_agent.network import PortalRouteError
        self.repo.enqueue('9900091')
        engine, client = self.engine()
        with patch.object(engine.network, 'preflight', side_effect=PortalRouteError('Local DNS or config failed')), \
             patch.object(client, 'offline', side_effect=AssertionError('Do not acknowledge a blocked batch')), \
             patch.object(engine.vpn, 'connect', side_effect=AssertionError('Do not connect a blocked batch')):
            for _ in range(6):
                self.assertFalse(engine.cycle())
        with self.repo.transaction() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE app_no='9900091'").fetchone()
            self.assertEqual(job['state'], 'queued')
            self.assertEqual(job['attempts'], 0)
            self.assertIsNone(job['worker_id'])
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM attempts').fetchone()['n'], 0)
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertEqual(self.repo.list('jobs')['items'][0]['attempts'], 1)

    def test_repair_only_claim_does_not_reserve_portal_jobs_or_reset_reservations(self):
        job = self.reserve('9900092')
        self.assertEqual(self.repo.claim(self.worker['id'], 5, repair_only=True), [])
        worker = self.repo.list('workers')['items'][0]
        self.assertEqual(worker['phase'], 'reserved')
        self.assertIsNotNone(worker['offline_until'])
        self.assertEqual(self.repo.claim(self.worker['id'], 5)[0]['reservation_token'], job['reservation_token'])
        self.repo.enqueue('9900093')
        second = self.repo.create_worker('repair-only-worker')
        self.assertEqual(self.repo.claim(second['id'], 5, repair_only=True), [])
        with self.repo.transaction() as conn:
            self.assertEqual(conn.execute("SELECT attempts FROM jobs WHERE app_no='9900093'").fetchone()['attempts'], 0)
        response = self.app.test_client().post('/api/control/v1/worker/claim',
                    json={'limit': 5, 'repair_only': 'true'}, headers={'Authorization': 'Bearer ' + second['token']})
        self.assertEqual(response.status_code, 400)

    def test_previous_day_assignment_is_still_claimable(self):
        job = self.reserve()
        with self.repo.transaction() as conn:
            conn.execute("UPDATE jobs SET due_at=now()-interval '1 day' WHERE id=%s", (job['id'],))
        self.assertEqual(self.repo.claim(self.worker['id'], 1)[0]['id'], job['id'])

    def test_expiry_reassigns_and_late_result_cannot_overwrite(self):
        old = self.reserve()
        with self.repo.transaction() as conn:
            conn.execute("UPDATE jobs SET expires_at=now()-interval '2 hours' WHERE id=%s", (old['id'],))
        self.repo.dispatch()
        second = self.repo.create_worker('replacement')
        new = self.repo.claim(second['id'], 1)[0]
        self.assertNotEqual(new['reservation_token'], old['reservation_token'])
        self.assertEqual(self.repo.complete(second['id'], self.body(new))['disposition'], 'accepted')
        self.assertEqual(self.repo.complete(self.worker['id'], self.body(old))['disposition'], 'obsolete')

    def test_completion_idempotency_and_identity_conflict(self):
        body = self.body(self.reserve())
        first = self.repo.complete(self.worker['id'], body)
        self.assertEqual(first, self.repo.complete(self.worker['id'], body))
        body['data']['Status'] = 'Changed'
        with self.assertRaises(Conflict):
            self.repo.complete(self.worker['id'], body)
        self.assertEqual(self.repo.list('results')['total'], 1)

    def test_missing_artifact_prevents_success_and_schedule_advance(self):
        body = self.body(self.reserve(), [{'sha256': digest(b'pdf'), 'size': 3, 'filename': 'test.pdf', 'kind': 'document'}])
        with self.assertRaises(Conflict):
            self.repo.complete(self.worker['id'], body)
        self.assertIsNone(self.repo.list('trademarks')['items'][0]['last_successful_fetch_at'])

    def test_partial_result_does_not_advance_successful_schedule(self):
        body = self.body(self.reserve())
        body['completeness'] = 'partial'
        body['warnings'] = ['Missing document']
        self.assertEqual(self.repo.complete(self.worker['id'], body)['disposition'], 'partial')
        self.assertIsNone(self.repo.list('trademarks')['items'][0]['last_successful_fetch_at'])
        self.assertEqual(self.repo.list('jobs')['items'][0]['state'], 'retry_wait')

    def test_long_term_policy_stays_out_of_queue_until_due(self):
        self.repo.create_policy('long_term', 'Explicit long-term policy', 3650)
        self.repo.schedule('7777777', 'long_term', utcnow() + dt.timedelta(days=3650))
        self.repo.dispatch()
        self.assertEqual(self.repo.list('jobs')['total'], 0)

    def test_request_consumption_and_shared_application_dedup(self):
        with self.repo.transaction() as conn:
            for user in (1, 2):
                conn.execute('INSERT INTO public.fleet_fetch_requests(id,app_no,user_id,reason) VALUES(%s,%s,%s,%s)',
                             (str(uuid.uuid4()), '1234567', user, 'initial'))
        self.repo.dispatch()
        self.repo.dispatch()
        self.assertEqual(self.repo.list('jobs')['total'], 1)

    def make_due(self, *app_nos):
        past = utcnow() - dt.timedelta(hours=1)
        for app_no in app_nos:
            self.repo.schedule(app_no, 'four_weeks', past)

    def request(self, app_no, priority, reason='manual'):
        with self.repo.transaction() as conn:
            conn.execute('INSERT INTO public.fleet_fetch_requests(id,app_no,user_id,reason,priority) VALUES(%s,%s,%s,%s,%s)',
                         (str(uuid.uuid4()), app_no, 1, reason, priority))

    def jobs_by_app(self):
        return {j['app_no']: j for j in self.repo.list('jobs', 100, 0)['items']}

    def test_due_work_is_queued_automatically_at_priority_zero(self):
        self.make_due('1000001', '1000002')
        self.repo.schedule('1000003', 'four_weeks', utcnow() + dt.timedelta(days=5))  # not due yet
        self.repo.dispatch()
        jobs = self.jobs_by_app()
        self.assertEqual(set(jobs), {'1000001', '1000002'})
        self.assertTrue(all(j['reason'] == 'scheduled' and j['priority'] == 0 for j in jobs.values()))

    def test_claim_reserves_the_highest_priority_work_first(self):
        self.make_due('2000001', '2000002', '2000003')
        self.request('2000004', 100, 'initial')
        self.request('2000005', 150)
        self.repo.dispatch()
        first = {j['app_no'] for j in self.repo.claim(self.worker['id'], 2)}
        self.assertEqual(first, {'2000005', '2000004'})  # the 150 and the 100 beat three scheduled jobs at 0
        rest = {j['app_no'] for j in self.repo.claim(self.worker['id'], 5)}
        self.assertEqual(rest, first)  # outstanding reservation is returned until it is finished
        for job in self.repo.claim(self.worker['id'], 5):
            self.repo.complete(self.worker['id'], self.body(job))
        remaining = {j['app_no'] for j in self.repo.claim(self.worker['id'], 5)}
        self.assertEqual(remaining, {'2000001', '2000002', '2000003'})

    def test_equal_priority_work_is_served_oldest_due_first(self):
        past = utcnow()
        for offset, app_no in ((3, '3000003'), (1, '3000001'), (2, '3000002')):
            self.repo.schedule(app_no, 'four_weeks', past - dt.timedelta(days=offset))
        self.repo.dispatch()
        self.assertEqual([j['app_no'] for j in self.repo.claim(self.worker['id'], 1)], ['3000003'])

    def test_a_user_request_raises_the_priority_of_work_already_queued(self):
        self.make_due('4000001')
        self.repo.dispatch()
        self.assertEqual(self.jobs_by_app()['4000001']['priority'], 0)
        self.request('4000001', 150)
        self.repo.dispatch()
        jobs = self.repo.list('jobs', 100, 0)
        self.assertEqual(jobs['total'], 1)
        self.assertEqual(jobs['items'][0]['priority'], 150)

    def add_owner(self, email, active, *tms):
        """tms: (app_no, priority) pairs owned by one portfolio user."""
        with self.repo.transaction() as conn:
            user = conn.execute('INSERT INTO public.users(email,password_hash,is_active) VALUES(%s,%s,%s) RETURNING id',
                                (email, 'x', active)).fetchone()['id']
            for app_no, priority in tms:
                conn.execute('INSERT INTO public.my_trademarks(app_no,user_id,priority) VALUES(%s,%s,%s)', (app_no, user, priority))
        return user

    def clear_owners(self):
        with self.repo.transaction() as conn:
            conn.execute('DELETE FROM public.my_trademarks')
            conn.execute('DELETE FROM public.users')

    def test_scheduled_work_carries_the_owners_priority(self):
        self.clear_owners()
        self.addCleanup(self.clear_owners)
        self.add_owner('added-in-ui@example.test', True, ('5000001', 100), ('5000002', 0))
        self.make_due('5000001', '5000002', '5000003')   # 5000003 is tracked by nobody
        self.repo.dispatch()
        jobs = self.jobs_by_app()
        self.assertEqual({a: j['priority'] for a, j in jobs.items()}, {'5000001': 100, '5000002': 0, '5000003': 0})
        self.assertEqual([j['app_no'] for j in self.repo.claim(self.worker['id'], 1)], ['5000001'])

    def test_trademarks_of_deactivated_users_are_not_queued(self):
        self.clear_owners()
        self.addCleanup(self.clear_owners)
        self.add_owner('gone@example.test', False, ('6000001', 100), ('6000002', 100))
        self.add_owner('here@example.test', True, ('6000003', 0))
        self.make_due('6000001', '6000002', '6000003')
        self.repo.dispatch()
        self.assertEqual(set(self.jobs_by_app()), {'6000003'})
        self.assertEqual(self.repo.overview()['overdue'], 1)  # the skipped ones do not count as overdue work

    def test_a_shared_application_is_queued_if_any_owner_is_active_with_that_owners_priority(self):
        self.clear_owners()
        self.addCleanup(self.clear_owners)
        self.add_owner('gone@example.test', False, ('7000001', 150))
        self.add_owner('here@example.test', True, ('7000001', 100))
        self.make_due('7000001')
        self.repo.dispatch()
        self.assertEqual(self.jobs_by_app()['7000001']['priority'], 100)  # the inactive owner's 150 is ignored

    def test_reactivating_a_user_brings_their_work_back(self):
        self.clear_owners()
        self.addCleanup(self.clear_owners)
        user = self.add_owner('back@example.test', False, ('8000001', 100))
        self.make_due('8000001')
        self.repo.dispatch()
        self.assertEqual(self.repo.list('jobs', 100, 0)['total'], 0)
        with self.repo.transaction() as conn:
            conn.execute('UPDATE public.users SET is_active=true WHERE id=%s', (user,))
        self.repo.dispatch()
        self.assertEqual(self.jobs_by_app()['8000001']['priority'], 100)

    def test_owner_view_exposes_no_personal_columns(self):
        with self.repo.transaction() as conn:
            columns = [r['column_name'] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='fleet_control' AND table_name='tm_owner_priority' ORDER BY ordinal_position").fetchall()]
        self.assertEqual(columns, ['app_no', 'priority', 'has_active_owner'])

    def fetched(self, app_no, status):
        job = self.reserve(app_no)
        body = self.body(job)
        body['data']['Status'] = status
        self.assertEqual(self.repo.complete(self.worker['id'], body)['disposition'], 'accepted')

    def marks(self):
        return {t['app_no']: t for t in self.repo.list('trademarks', 100, 0)['items']}

    def days(self, mark):
        return None if mark['next_fetch_at'] is None else (mark['next_fetch_at'] - mark['last_successful_fetch_at']).days

    def test_recommended_status_days_cover_all_49_statuses_and_drive_the_schedule(self):
        from control_plane.statuses import STATUS_DEFAULTS
        self.assertEqual(len(STATUS_DEFAULTS), 49)
        self.assertEqual(len({s.lower() for s, _ in STATUS_DEFAULTS}), 49)
        self.assertEqual(self.repo.load_status_defaults(), {'created': 49, 'kept': 0, 'updated_trademarks': 0})
        self.assertEqual(self.repo.load_status_defaults()['created'], 0)  # nothing left to fill the second time
        expected = (('9100001', 'Registered', 90), ('9100002', 'Opposed', 7), ('9100003', 'Opposed*', 7),
                    ('9100004', 'Ready for Examination', 14), ('9100005', 'Refused', 30),
                    ('9100006', 'Abandoned', 180), ('9100007', 'Withdrawn', None))
        for app_no, status, _ in expected:
            self.fetched(app_no, status)
        marks = self.marks()
        for app_no, status, days in expected:
            self.assertEqual(self.days(marks[app_no]), days, status)
            self.assertTrue(marks[app_no]['policy_id'].startswith('st_'), status)

    def test_changing_a_status_interval_moves_every_trademark_in_that_status(self):
        self.fetched('9200001', 'Formalities Chk Pass')
        self.fetched('9200002', 'Formalities Chk Pass')
        self.fetched('9200003', 'Formalities Chk Pass')
        self.fetched('9200004', 'Registered')
        self.repo.schedule('9200003', 'weekly', utcnow() + dt.timedelta(days=2))  # hand-set schedule: must stay
        result = self.repo.set_status_interval('Formalities Chk Pass', 3)
        self.assertEqual(result['updated_trademarks'], 2)
        marks = self.marks()
        self.assertEqual([self.days(marks['9200001']), self.days(marks['9200002'])], [3, 3])
        self.assertEqual(marks['9200001']['policy_id'], 'st_formalities_chk_pass')
        self.assertEqual(marks['9200003']['policy_id'], 'weekly')           # manual schedule untouched
        self.assertEqual(marks['9200004']['policy_id'], 'four_weeks')       # other status untouched
        self.assertEqual(self.repo.set_status_interval('formalities chk pass', 5)['updated_trademarks'], 2)  # any case
        self.assertEqual(self.days(self.marks()['9200001']), 5)
        self.repo.set_status_interval('Formalities Chk Pass', None)          # never fetch automatically
        self.assertIsNone(self.marks()['9200001']['next_fetch_at'])
        # a trademark that later enters the status picks the rule up on its next completed fetch
        self.repo.set_status_interval('Formalities Chk Pass', 3)
        self.fetched('9200005', 'Formalities Chk Pass')
        self.assertEqual(self.days(self.marks()['9200005']), 3)

    def test_shortening_a_status_makes_older_trademarks_due_now(self):
        self.fetched('9300001', 'Opposed')
        with self.repo.transaction() as conn:
            conn.execute("UPDATE trademarks SET last_successful_fetch_at=now()-interval '5 days'")
        self.repo.set_status_interval('Opposed', 3)
        self.repo.dispatch()
        self.assertEqual({j['app_no'] for j in self.repo.list('jobs', 100, 0)['items']}, {'9300001', })

    def test_status_schedule_view_counts_trademarks_and_flags_unlisted_statuses(self):
        self.fetched('9400001', 'Opposed')
        self.fetched('9400002', 'Opposed')
        self.fetched('9400003', 'Brand New Portal Status')
        view = self.repo.status_schedule()
        rows = {r['status']: r for r in view['items']}
        self.assertEqual(len([r for r in view['items'] if r['known']]), 49)
        self.assertEqual(rows['Opposed']['trademarks'], 2)
        self.assertFalse(rows['Opposed']['has_rule'])
        self.assertEqual(rows['Opposed']['recommended_days'], 7)
        self.assertFalse(rows['Brand New Portal Status']['known'])
        self.assertEqual(view['trademarks_total'], 3)
        self.assertEqual(view['known_status_trademarks'], 3)
        self.repo.set_status_interval('Brand New Portal Status', 21)  # unlisted but seen: allowed
        self.assertEqual({r['status']: r for r in self.repo.status_schedule()['items']}['Brand New Portal Status']['interval_days'], 21)

    def test_load_recommended_days_keeps_rules_an_admin_already_changed(self):
        self.repo.set_status_interval('Registered', 45)
        result = self.repo.load_status_defaults()
        self.assertEqual((result['created'], result['kept']), (48, 1))
        rows = {r['status']: r for r in self.repo.status_schedule()['items']}
        self.assertEqual(rows['Registered']['interval_days'], 45)
        self.assertEqual(rows['Opposed']['interval_days'], 7)

    def test_status_schedule_rejects_bad_input(self):
        for status, days in (('', 7), ('Not A Real Status', 7), ('Opposed', 0), ('Opposed', -1), ('Opposed', 'soon'), ('Opposed', 7.5), (None, 7)):
            with self.assertRaises(ValueError, msg=(status, days)):
                self.repo.set_status_interval(status, days)

    def test_status_schedule_admin_api_and_schedule_post_alias(self):
        client = self.app.test_client()
        auth = {'Authorization': 'Bearer ' + self.settings.admin_token}
        self.assertEqual(client.get('/api/control/v1/admin/status-schedule').status_code, 401)
        listing = client.get('/api/control/v1/admin/status-schedule', headers=auth).get_json()
        self.assertEqual(listing['total'], 49)
        saved = client.post('/api/control/v1/admin/status-schedule', json={'status': 'Formalities Chk Pass', 'interval_days': 3}, headers=auth)
        self.assertEqual((saved.status_code, saved.get_json()['interval_days']), (200, 3))
        self.assertEqual(client.post('/api/control/v1/admin/status-schedule', json={'status': 'Opposed', 'interval_days': 0}, headers=auth).status_code, 400)
        self.assertEqual(client.post('/api/control/v1/admin/status-schedule/defaults', json={}, headers=auth).get_json()['created'], 48)
        # POST works for schedules because the user backend's admin proxy does not forward PUT
        body = {'policy_id': 'weekly', 'next_fetch_at': (utcnow() + dt.timedelta(days=1)).isoformat()}
        self.assertEqual(client.post('/api/control/v1/admin/schedules/9500001', json=body, headers=auth).status_code, 200)
        self.assertEqual(client.put('/api/control/v1/admin/schedules/9500002', json=body, headers=auth).status_code, 200)

    def test_machine_summary_is_plain_and_never_leaks_credentials(self):
        from cryptography.fernet import Fernet
        repo = Repository(replace(self.settings, account_key=Fernet.generate_key().decode()))
        repo.add_account({'email': 'portal.owner@example.test', 'app_password': 'never-shown-secret', 'worker_id': self.worker['id']})
        job = self.reserve('9600001')
        self.repo.complete(self.worker['id'], self.body(job))
        failing = self.reserve('9600002')
        self.repo.fail(self.worker['id'], {'job_id': failing['id'], 'reservation_token': failing['reservation_token'], 'error': 'Portal timed out'})
        summary = repo.worker_summary(self.worker['id'])
        self.assertEqual(summary['worker']['name'], 'test-worker')
        self.assertEqual(summary['accounts'], [{'id': summary['accounts'][0]['id'], 'email': 'por***@example.test', 'enabled': True}])
        self.assertEqual(summary['result_totals'], {'accepted': 1})
        self.assertEqual([r['app_no'] for r in summary['recent_results']], ['9600001'])
        self.assertEqual(summary['recent_problems'][0]['last_error'], 'Portal timed out')
        text = str(summary)
        for secret in ('never-shown-secret', 'portal.owner', 'token_hash', self.worker['token']):
            self.assertNotIn(secret, text)
        client = self.app.test_client()
        auth = {'Authorization': 'Bearer ' + self.settings.admin_token}
        self.assertEqual(client.get(f"/api/control/v1/admin/workers/{self.worker['id']}/summary", headers=auth).status_code, 200)
        self.assertEqual(client.get(f"/api/control/v1/admin/workers/{self.worker['id']}/summary").status_code, 401)
        self.assertEqual(client.get('/api/control/v1/admin/workers/not-a-real-id/summary', headers=auth).status_code, 400)

    def test_accounts_overview_names_machines_and_bulk_assign_reports_each_account(self):
        from cryptography.fernet import Fernet
        repo = Repository(replace(self.settings, account_key=Fernet.generate_key().decode()))
        second = repo.create_worker('Office PC 2')
        ids = [repo.add_account({'email': f'acct{i}@example.test', 'app_password': 'x'}) for i in range(4)]
        overview = repo.accounts_overview()
        self.assertEqual((overview['total'], overview['assigned'], overview['unassigned']), (4, 0, 4))
        self.assertEqual({m['name'] for m in overview['machines']}, {'test-worker', 'Office PC 2'})
        result = repo.assign_accounts(ids[:3], self.worker['id'])
        self.assertEqual((len(result['assigned']), result['failed'], result['machine']), (3, [], 'test-worker'))
        overview = repo.accounts_overview()
        self.assertEqual({a['email']: a['worker_name'] for a in overview['accounts'] if a['worker_id']},
                         {f'acct{i}@example.test': 'test-worker' for i in range(3)})
        self.assertEqual({m['name']: m['accounts'] for m in overview['machines']}, {'test-worker': 3, 'Office PC 2': 0})
        # moving accounts away from a machine that holds a batch is refused, per account, in plain words
        self.reserve('9700001')
        moved = repo.assign_accounts([ids[0], ids[3]], second['id'])
        self.assertEqual(moved['assigned'], [ids[3]])
        self.assertEqual([f['id'] for f in moved['failed']], [ids[0]])
        self.assertIn('Finish and stop', moved['failed'][0]['reason'])
        # disable several at once, then take them back from any machine
        self.assertEqual(repo.assign_accounts([ids[3]], second['id'], False)['failed'], [])
        self.assertFalse({a['id']: a for a in repo.accounts_overview()['accounts']}[ids[3]]['enabled'])
        self.assertEqual(repo.assign_accounts([ids[3]], None)['machine'], None)
        # enabling/disabling without naming a machine keeps each account where it is...
        repo.assign_accounts([ids[3]], second['id'])
        repo.assign_accounts([ids[3]], None, False, keep_machine=True)
        kept = {a['id']: a for a in repo.accounts_overview()['accounts']}[ids[3]]
        self.assertEqual((kept['worker_name'], kept['enabled']), ('Office PC 2', False))
        # ...and a machine that is holding a batch cannot lose or disable an account yet
        busy = repo.assign_accounts([ids[1]], None, False, keep_machine=True)
        self.assertEqual(busy['assigned'], [])
        self.assertIn('Finish and stop', busy['failed'][0]['reason'])

    def test_accounts_cannot_be_given_to_missing_or_removed_machines_or_empty_selections(self):
        from cryptography.fernet import Fernet
        repo = Repository(replace(self.settings, account_key=Fernet.generate_key().decode()))
        account = repo.add_account({'email': 'solo@example.test', 'app_password': 'x'})
        gone = repo.create_worker('Retired PC')
        repo.update_worker(gone['id'], {'status': 'retired', 'capacity': 5})
        for ids, worker in (([], self.worker['id']), (None, self.worker['id']), ([account], 'not-an-id'),
                            ([account], str(uuid.uuid4())), ([account], gone['id'])):
            with self.assertRaises(ValueError, msg=(ids, worker)):
                repo.assign_accounts(ids, worker)

    def test_accounts_admin_api_serves_overview_and_bulk_assign(self):
        from cryptography.fernet import Fernet
        settings = replace(self.settings, account_key=Fernet.generate_key().decode())
        app = create_app(settings); app.testing = True
        client, auth = app.test_client(), {'Authorization': 'Bearer ' + settings.admin_token}
        account = Repository(settings).add_account({'email': 'api@example.test', 'app_password': 'x'})
        self.assertEqual(client.get('/api/control/v1/admin/accounts-overview').status_code, 401)
        self.assertEqual(client.get('/api/control/v1/admin/accounts-overview', headers=auth).get_json()['total'], 1)
        reply = client.post('/api/control/v1/admin/accounts/assign', json={'account_ids': [account], 'worker_id': self.worker['id']}, headers=auth)
        self.assertEqual((reply.status_code, reply.get_json()['assigned']), (200, [account]))
        self.assertEqual(client.post('/api/control/v1/admin/accounts/assign', json={'account_ids': [], 'worker_id': None}, headers=auth).status_code, 400)

    def build_queue(self):
        done = self.reserve('9800001')
        body = self.body(done)
        body['data']['Trade Mark'] = 'ACME TEA'
        self.repo.complete(self.worker['id'], body)
        dead = self.reserve('9800002')
        self.repo.fail(self.worker['id'], {'job_id': dead['id'], 'reservation_token': dead['reservation_token'], 'error': 'Portal timed out', 'retryable': False})
        later = self.reserve('9800003')
        self.repo.fail(self.worker['id'], {'job_id': later['id'], 'reservation_token': later['reservation_token'], 'error': 'Portal timed out'})
        self.reserve('9800005')                 # held by the machine right now
        self.repo.enqueue('9800004')            # waiting for a machine

    def test_queue_overview_groups_jobs_the_way_staff_think_about_them(self):
        self.build_queue()
        view = self.repo.queue_overview()
        self.assertEqual([j['app_no'] for j in view['items']], ['9800002', '9800003', '9800005', '9800004'])  # problems first
        self.assertEqual(view['counts'], {'waiting': 1, 'active': 1, 'retrying': 1, 'failed': 1, 'done_recently': 1, 'cancelled': 0})
        self.assertEqual((view['total'], view['max_attempts']), (4, self.settings.max_attempts))
        held = {j['app_no']: j for j in view['items']}['9800005']
        self.assertEqual(held['worker_name'], 'test-worker')
        done = self.repo.queue_overview('done')
        self.assertEqual([(j['app_no'], j['mark']) for j in done['items']], [('9800001', 'ACME TEA')])
        for name, expected in (('waiting', ['9800004']), ('active', ['9800005']), ('retrying', ['9800003']), ('failed', ['9800002'])):
            self.assertEqual([j['app_no'] for j in self.repo.queue_overview(name)['items']], expected, name)
        self.assertEqual([j['app_no'] for j in self.repo.queue_overview('attention', limit=2, offset=2)['items']], ['9800005', '9800004'])
        with self.assertRaises(ValueError):
            self.repo.queue_overview('everything')

    def test_staff_can_queue_many_trademarks_at_once_with_a_simple_priority(self):
        result = self.repo.queue_trademarks('9810001, 9810002\n9810003 abc 9810001 12345678901234', 'high')
        self.assertEqual(result, {'queued': ['9810001', '9810002', '9810003'], 'already_waiting': [], 'invalid': ['abc', '12345678901234']})
        jobs = self.jobs_by_app()
        self.assertTrue(all(j['priority'] == 150 and j['reason'] == 'staff_request' for j in jobs.values()))
        again = self.repo.queue_trademarks(['9810001', '9810009'], 'top')
        self.assertEqual((again['queued'], again['already_waiting']), (['9810009'], ['9810001']))
        self.assertEqual(self.jobs_by_app()['9810001']['priority'], 500)  # moved up, not duplicated
        self.assertEqual(self.repo.list('jobs', 100, 0)['total'], 4)
        for numbers, priority in (('', 'normal'), (None, 'normal'), ('9810001', 'urgent'), (' '.join(str(9900000 + i) for i in range(501)), 'normal')):
            with self.assertRaises(ValueError, msg=(numbers, priority)):
                self.repo.queue_trademarks(numbers, priority)

    def test_queue_admin_api(self):
        self.build_queue()
        client = self.app.test_client()
        auth = {'Authorization': 'Bearer ' + self.settings.admin_token}
        self.assertEqual(client.get('/api/control/v1/admin/queue-overview').status_code, 401)
        view = client.get('/api/control/v1/admin/queue-overview?view=failed', headers=auth).get_json()
        self.assertEqual([j['app_no'] for j in view['items']], ['9800002'])
        self.assertEqual(client.get('/api/control/v1/admin/queue-overview?view=bogus', headers=auth).status_code, 400)
        reply = client.post('/api/control/v1/admin/queue', json={'app_nos': '9820001 9820002', 'priority': 'normal'}, headers=auth)
        self.assertEqual((reply.status_code, reply.get_json()['queued']), (200, ['9820001', '9820002']))
        self.assertEqual(client.post('/api/control/v1/admin/queue', json={'app_nos': '', 'priority': 'normal'}, headers=auth).status_code, 400)

    def worker_row(self, worker_id):
        with self.repo.transaction() as conn:
            return conn.execute('SELECT * FROM workers WHERE id=%s', (worker_id,)).fetchone()

    def test_enrollment_key_creates_a_waiting_machine_with_its_own_credential(self):
        made = self.repo.create_enrollment_key(7, max_uses=2, label='Office rollout')
        self.assertTrue(made['key'].startswith('tmk_'))
        listed = self.repo.list_enrollment_keys()
        self.assertEqual([(k['label'], k['uses'], k['max_uses']) for k in listed], [('Office rollout', 0, 2)])
        self.assertNotIn(made['key'], str(listed))  # only a hash is ever stored
        first = self.repo.enroll_worker(made['key'], 'OFFICE-PC-7')
        row = self.worker_row(first['worker_id'])
        self.assertEqual((row['status'], row['name'], row['enrolled_hostname']), ('pending', 'OFFICE-PC-7', 'OFFICE-PC-7'))
        second = self.repo.enroll_worker(made['key'], 'OFFICE-PC-7')
        self.assertEqual(self.worker_row(second['worker_id'])['name'], 'OFFICE-PC-7 (2)')
        with self.assertRaises(PermissionError):  # the key allowed two machines
            self.repo.enroll_worker(made['key'], 'THIRD-PC')
        self.assertEqual(self.repo.list_enrollment_keys()[0]['uses'], 2)
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(self.repo.authenticate_worker(first['token'])['id'], first['worker_id'])

    def test_a_default_key_never_expires_and_has_no_machine_limit_until_revoked_by_hand(self):
        made = self.repo.create_enrollment_key()
        self.assertEqual((made['expires_at'], made['max_uses']), (None, None))
        for n in range(6):  # many machines, no automatic cut-off
            self.repo.enroll_worker(made['key'], f'PC-{n}')
        listed = self.repo.list_enrollment_keys()[0]
        self.assertEqual((listed['expires_at'], listed['max_uses'], listed['uses'], listed['revoked_at']), (None, None, 6, None))
        self.repo.revoke_enrollment_key(made['id'])  # revoking is a manual decision
        with self.assertRaises(PermissionError):
            self.repo.enroll_worker(made['key'], 'LATE')
        via_api = self.app.test_client().post('/api/control/v1/admin/enrollment-keys', json={'label': 'No limits'},
                                              headers={'Authorization': 'Bearer ' + self.settings.admin_token}).get_json()
        self.assertIsNone(via_api['expires_at'])

    def test_bad_revoked_and_expired_enrollment_keys_are_refused(self):
        for key in ('', None, 'tmk_made-up', 12345):
            with self.assertRaises(PermissionError, msg=key):
                self.repo.enroll_worker(key, 'X')
        revoked = self.repo.create_enrollment_key()
        self.repo.revoke_enrollment_key(revoked['id'])
        with self.assertRaises(PermissionError):
            self.repo.enroll_worker(revoked['key'], 'X')
        expired = self.repo.create_enrollment_key()
        with self.repo.transaction() as conn:
            conn.execute("UPDATE enrollment_keys SET expires_at=now()-interval '1 minute' WHERE id=%s", (expired['id'],))
        with self.assertRaises(PermissionError):
            self.repo.enroll_worker(expired['key'], 'X')
        for days, uses in ((0, None), (3651, None), (7, 0), (7, 5000), ('7', None)):
            with self.assertRaises(ValueError, msg=(days, uses)):
                self.repo.create_enrollment_key(days, uses)
        with self.assertRaises(ValueError):
            self.repo.revoke_enrollment_key(str(uuid.uuid4()))

    def test_enrolled_hostnames_are_cleaned(self):
        key = self.repo.create_enrollment_key()['key']
        worker = self.repo.enroll_worker(key, '../evil<script>alert(1)</script>-PC')
        name = self.worker_row(worker['worker_id'])['name']
        self.assertFalse(set('<>/\\()') & set(name), name)
        blank = self.repo.enroll_worker(key, '   ')
        self.assertEqual(self.worker_row(blank['worker_id'])['name'], 'new-machine')

    def test_a_waiting_machine_gets_no_work_and_no_accounts_until_approved(self):
        from cryptography.fernet import Fernet
        repo = Repository(replace(self.settings, account_key=Fernet.generate_key().decode()))
        waiting = repo.enroll_worker(repo.create_enrollment_key()['key'], 'NEW-PC')
        self.repo.enqueue('9990001')
        self.assertEqual(repo.claim(waiting['worker_id'], 5), [])
        self.assertEqual(repo.configuration(waiting['worker_id'])['accounts'], [])
        account = repo.add_account({'email': 'free@example.test', 'app_password': 'x'})
        self.assertNotIn(waiting['worker_id'], [m['id'] for m in repo.accounts_overview()['machines']])
        with self.assertRaises(ValueError):
            repo.assign_accounts([account], waiting['worker_id'])
        repo.approve_worker(waiting['worker_id'], 'Office PC 7', 5)
        self.assertEqual(len(repo.claim(waiting['worker_id'], 5)), 1)
        self.assertEqual(repo.assign_accounts([account], waiting['worker_id'])['failed'], [])

    def test_approving_names_the_machine_and_rejecting_removes_it(self):
        repo = self.repo
        key = repo.create_enrollment_key()['key']
        a, b = repo.enroll_worker(key, 'PC-A'), repo.enroll_worker(key, 'PC-B')
        with self.assertRaises(ValueError):
            repo.update_worker(a['worker_id'], {'status': 'active', 'capacity': 5})  # must go through approval
        self.assertEqual(repo.approve_worker(a['worker_id'], '  Reception PC  ', 12)['name'], 'Reception PC')
        row = self.worker_row(a['worker_id'])
        self.assertEqual((row['status'], row['name'], row['capacity']), ('active', 'Reception PC', 12))
        with self.assertRaises(ValueError):
            repo.approve_worker(a['worker_id'], 'Reception PC', 5)          # already approved
        with self.assertRaises(ValueError):
            repo.approve_worker(b['worker_id'], 'reception pc', 5)          # name taken (case-insensitive)
        for bad_name, cap in (('', 5), ('x' * 81, 5), ('Fine', 0), ('Fine', 101)):
            with self.assertRaises(ValueError, msg=(bad_name, cap)):
                repo.approve_worker(b['worker_id'], bad_name, cap)
        repo.update_worker(b['worker_id'], {'status': 'retired', 'capacity': 5})  # reject
        self.assertIsNone(repo.authenticate_worker(b['token']))

    def test_enrollment_over_http_needs_no_login_and_is_rate_limited(self):
        client = self.app.test_client()
        admin = {'Authorization': 'Bearer ' + self.settings.admin_token}
        for method, path in (('get', '/api/control/v1/admin/enrollment-keys'), ('post', '/api/control/v1/admin/enrollment-keys'),
                             ('post', '/api/control/v1/admin/workers/%s/approve' % uuid.uuid4())):
            self.assertEqual(getattr(client, method)(path).status_code, 401, path)
        made = client.post('/api/control/v1/admin/enrollment-keys', json={'days': 3, 'max_uses': 5, 'label': 'Rollout'}, headers=admin)
        self.assertEqual(made.status_code, 201)
        key = made.get_json()['key']
        reply = client.post('/api/control/v1/enroll', json={'key': key, 'hostname': 'HTTP-PC'})
        self.assertEqual(reply.status_code, 201)
        token, worker_id = reply.get_json()['token'], reply.get_json()['worker_id']
        beat = client.post('/api/control/v1/worker/heartbeat', json={'phase': 'idle', 'metadata': {}}, headers={'Authorization': 'Bearer ' + token})
        self.assertEqual((beat.status_code, beat.get_json()['worker_status']), (200, 'pending'))
        claim = client.post('/api/control/v1/worker/claim', json={'limit': 5}, headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(claim.get_json()['assignments'], [])
        approved = client.post(f'/api/control/v1/admin/workers/{worker_id}/approve', json={'name': 'Front desk', 'capacity': 8}, headers=admin)
        self.assertEqual((approved.status_code, approved.get_json()['name']), (200, 'Front desk'))
        listing = client.get('/api/control/v1/admin/enrollment-keys', headers=admin).get_json()['items']
        self.assertEqual((listing[0]['uses'], 'key' in listing[0]), (1, False))
        self.assertEqual(client.post(f"/api/control/v1/admin/enrollment-keys/{listing[0]['id']}/revoke", headers=admin).status_code, 200)
        self.assertEqual(client.post('/api/control/v1/enroll', json={'key': key, 'hostname': 'LATE-PC'}).status_code, 403)
        for _ in range(25):
            last = client.post('/api/control/v1/enroll', json={'key': 'tmk_wrong', 'hostname': 'ATTACKER'})
        self.assertEqual(last.status_code, 429)
        self.assertEqual(client.post('/api/control/v1/enroll', json={'key': key, 'hostname': 'X'}).status_code, 429)

    def test_worker_cannot_use_admin_api_or_another_workers_reservation(self):
        client = self.app.test_client()
        response = client.get('/api/control/v1/admin/workers', headers={'Authorization': 'Bearer ' + self.worker['token']})
        self.assertEqual(response.status_code, 401)
        job = self.reserve()
        second = self.repo.create_worker('other')
        with self.assertRaises(Conflict):
            self.repo.complete(second['id'], self.body(job))

    def test_corrupt_upload_rejected(self):
        job = self.reserve()
        client = self.app.test_client()
        response = client.put(f"/api/control/v1/worker/artifacts/{uuid.uuid4()}/{digest(b'expected')}", data=b'bad',
                              headers={'Authorization': 'Bearer ' + self.worker['token'], 'X-Job-ID': job['id'],
                                       'X-Reservation-Token': job['reservation_token']})
        self.assertEqual(response.status_code, 400)

    def engine(self):
        vpn = FixtureVPN()
        client = FlaskTransport(self.app, self.worker['token'], vpn)
        settings = WorkerSettings('http://127.0.0.1:5010', self.worker['token'], 'test-local-worker-credential', self.root / 'worker')
        return WorkerEngine(settings, client=client, vpn=vpn), client

    def test_end_to_end_offline_cycle_and_artifact_delivery(self):
        self.repo.enqueue('1234567')
        engine, _ = self.engine()
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertEqual(engine.store.counts().get('acknowledged'), 1)
        self.assertEqual(self.repo.list('results')['items'][0]['disposition'], 'accepted')
        self.assertTrue(any(self.settings.artifact_dir.rglob('*')))

    def split_engine(self):
        from worker_agent.network import FixtureNetwork
        original, client = self.engine()
        settings = replace(original.settings, network_mode='split')
        client.allow_vpn = True
        engine = WorkerEngine(settings, client=client, vpn=original.vpn, network=FixtureNetwork(settings))
        return engine, client

    def test_split_cycle_delivers_and_heartbeats_while_tunnel_active(self):
        self.repo.enqueue('1234567')
        engine, client = self.split_engine()
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertIn('/heartbeat', client.vpn_calls)
        self.assertIn('/complete', client.vpn_calls)
        self.assertEqual(engine.store.counts()['acknowledged'], 1)
        self.assertFalse(engine.vpn.active)
        self.assertFalse(engine.monitor.is_alive())
        self.assertEqual(engine.status()['network']['effective_mode'], 'offline')
        self.assertTrue(engine.status()['network']['simulated'])

    def test_split_coordinator_outage_continues_reserved_batch_then_syncs(self):
        for app_no in ('1234567', '1234568'):
            self.repo.enqueue(app_no)
        engine, client = self.split_engine()
        client.fail_online = True
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertNotIn('/complete', client.vpn_calls)
        self.assertEqual(engine.store.counts()['acknowledged'], 2)
        self.assertEqual(self.repo.list('results')['total'], 2)

    def test_split_lost_ack_uses_same_saved_result_after_disconnect(self):
        self.repo.enqueue('1234567')
        engine, client = self.split_engine()
        client.lose_ack = True
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertEqual(self.repo.list('results')['total'], 1)
        self.assertEqual(client.vpn_calls.count('/complete'), 1)
        self.assertEqual(engine.store.counts()['acknowledged'], 1)

    def test_split_interrupted_upload_restart_does_not_rescrape(self):
        self.repo.enqueue('1234567')
        engine, client = self.split_engine()
        client.fail_completion = True
        self.assertFalse(engine.cycle())
        saved = engine.store.list(('upload_pending',))[0]['result']['result_id']
        client.fail_completion = False
        restarted = WorkerEngine(engine.settings, client=client, vpn=engine.vpn)
        self.assertTrue(restarted.cycle(), restarted.last_error)
        self.assertEqual(self.repo.list('results')['items'][0]['id'], saved)

    def test_split_route_failure_prevents_portal_start_and_disconnects(self):
        from worker_agent.network import PortalRouteError
        from unittest.mock import Mock
        self.repo.enqueue('1234567')
        engine, client = self.split_engine()
        engine.network.portal_check = Mock(side_effect=PortalRouteError('Wrong portal route'))
        engine.scraper.start = Mock()
        self.assertFalse(engine.cycle())
        engine.scraper.start.assert_not_called()
        self.assertFalse(engine.vpn.active)
        self.assertEqual(self.repo.list('results')['total'], 0)
        self.assertEqual(client.vpn_calls, [])

    def test_split_route_change_between_items_stops_remaining_scrapes(self):
        from worker_agent.network import PortalRouteError
        for app_no in ('1234567', '1234568'):
            self.repo.enqueue(app_no)
        engine, client = self.split_engine()
        original = engine.scraper.scrape
        def scrape(job):
            result = original(job)
            def blocked(url=None):
                raise PortalRouteError('Portal address changed')
            engine.network.portal_check = blocked
            return result
        engine.scraper.scrape = scrape
        self.assertFalse(engine.cycle())
        self.assertFalse(engine.vpn.active)
        self.assertEqual(self.repo.list('results')['total'], 1)
        self.assertEqual(engine.store.counts()['received'], 1)

    def test_split_confirmed_online_worker_is_not_expected_offline(self):
        job = self.reserve()
        self.repo.acknowledge_offline(self.worker['id'], [job])
        self.repo.heartbeat(self.worker['id'], 'scraping', {'network': {'effective_mode': 'split', 'coordinator': 'verified'}})
        self.assertEqual(self.repo.overview()['worker_connectivity']['online'], 1)
        self.repo.heartbeat(self.worker['id'], 'scraping', {'network': {'effective_mode': 'offline', 'coordinator': 'unreachable'}})
        self.assertEqual(self.repo.overview()['worker_connectivity']['expected_offline'], 1)

    def test_empty_claim_keeps_idle_worker_out_of_reserved_phase(self):
        self.assertEqual(self.repo.claim(self.worker['id'], 5), [])
        worker = self.repo.list('workers')['items'][0]
        self.assertEqual(worker['phase'], 'idle')
        self.assertIsNone(worker['offline_until'])

    def test_stopping_monitor_cannot_race_teardown_or_next_cycle(self):
        from unittest.mock import patch, Mock
        self.repo.enqueue('1234567')
        engine, client = self.split_engine()
        class Monitor:
            alive = True
            def start(self):
                pass
            def join(self, timeout):
                pass
            def is_alive(self):
                return self.alive
        monitor = Monitor()
        engine.scraper.stop = Mock()
        engine.scraper.close = Mock()
        with patch('worker_agent.engine.threading.Thread', return_value=monitor):
            self.assertFalse(engine.cycle())
        engine.scraper.stop.assert_called_once()
        self.assertTrue(engine.vpn.active)
        contacts = len(client.vpn_calls)
        self.assertFalse(engine.cycle())
        self.assertEqual(len(client.vpn_calls), contacts)
        monitor.alive = False
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertFalse(engine.vpn.active)
        self.assertEqual(self.repo.list('results')['total'], 1)

    def test_split_drain_during_scrape_prevents_next_item(self):
        for app_no in ('1234567', '1234568'):
            self.repo.enqueue(app_no)
        engine, client = self.split_engine()
        original = engine.scraper.scrape
        def scrape(job):
            result = original(job)
            self.repo.update_worker(self.worker['id'], {'status': 'draining', 'capacity': 5})
            engine._online_tick()
            return result
        engine.scraper.scrape = scrape
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertEqual(self.repo.list('results')['total'], 1)
        self.assertEqual(engine.store.counts()['received'], 1)

    def test_delivery_outage_and_restart_reuses_saved_result(self):
        self.repo.enqueue('1234567')
        engine, client = self.engine()
        client.fail_completion = True
        self.assertFalse(engine.cycle())
        saved = engine.store.list(('upload_pending',))[0]['result']['result_id']
        client.fail_completion = False
        restarted = WorkerEngine(engine.settings, client=client, vpn=engine.vpn)
        self.assertTrue(restarted.cycle(), restarted.last_error)
        self.assertEqual(self.repo.list('results')['items'][0]['id'], saved)
        self.assertEqual(self.repo.list('results')['total'], 1)

    def test_lost_ack_retries_without_rescraping(self):
        # One lost acknowledgment is retried inside the same cycle, from the same saved result.
        self.repo.enqueue('1234567')
        engine, client = self.engine()
        client.lose_ack = True
        with patch('time.sleep'):
            self.assertTrue(engine.cycle(), engine.last_error)
        self.assertEqual(self.repo.list('results')['total'], 1)
        self.assertEqual(engine.store.counts()['acknowledged'], 1)

    def test_outage_through_every_retry_is_delivered_next_cycle_without_rescraping(self):
        self.repo.enqueue('1234567')
        engine, client = self.engine()
        client.fail_completion = True
        with patch('time.sleep'):
            self.assertFalse(engine.cycle())
        self.assertEqual(engine.store.counts()['upload_pending'], 1)
        client.fail_completion = False
        self.assertTrue(engine.cycle(), engine.last_error)
        self.assertEqual(self.repo.list('results')['total'], 1)

    def test_revoked_worker_cannot_claim(self):
        self.repo.update_worker(self.worker['id'], {'status': 'retired', 'capacity': 5})
        response = self.app.test_client().post('/api/control/v1/worker/claim', json={'limit': 1}, headers={'Authorization': 'Bearer ' + self.worker['token']})
        self.assertEqual(response.status_code, 401)

    def test_accounts_are_encrypted_masked_and_scoped(self):
        from cryptography.fernet import Fernet
        repo = Repository(replace(self.settings, account_key=Fernet.generate_key().decode()))
        second = repo.create_worker('other-account-worker')
        account = repo.add_account({'email': 'fixture@example.invalid', 'app_password': 'fixture-secret', 'worker_id': self.worker['id']})
        with repo.transaction() as conn:
            stored = conn.execute('SELECT encrypted_credentials FROM accounts WHERE id=%s', (account,)).fetchone()
        self.assertNotIn('fixture-secret', stored['encrypted_credentials'])
        self.assertNotIn('encrypted_credentials', repo.list('accounts')['items'][0])
        self.assertEqual(repo.configuration(second['id'])['accounts'], [])
        self.assertEqual(repo.configuration(self.worker['id'])['accounts'][0]['app_password'], 'fixture-secret')
        self.reserve()
        with self.assertRaises(Conflict):
            repo.assign_account(account, {'worker_id': second['id']})

    def test_paused_schedule_remains_paused_after_manual_fetch(self):
        self.repo.schedule('1234567', 'paused', utcnow())
        self.repo.dispatch()
        self.assertEqual(self.repo.list('jobs')['total'], 0)
        job = self.reserve()
        self.repo.complete(self.worker['id'], self.body(job))
        self.assertIsNone(self.repo.list('trademarks')['items'][0]['next_fetch_at'])

    def test_status_policy_and_manual_override(self):
        self.repo.create_policy('registered_rule', 'Registered rule', 7, 'Registered')
        job = self.reserve()
        self.repo.complete(self.worker['id'], self.body(job))
        self.assertEqual(self.repo.list('trademarks')['items'][0]['policy_id'], 'registered_rule')
        self.repo.schedule('1234567', 'three_weeks', utcnow() + dt.timedelta(days=21))
        job = self.reserve()
        self.repo.complete(self.worker['id'], self.body(job))
        self.assertEqual(self.repo.list('trademarks')['items'][0]['policy_id'], 'three_weeks')

    def test_recovered_failed_generation_does_not_block_next_schedule(self):
        job = self.reserve()
        self.repo.fail(self.worker['id'], {'job_id': job['id'], 'reservation_token': job['reservation_token'], 'retryable': False})
        self.repo.edit_job(job['id'], {'action': 'retry'})
        retry = self.repo.claim(self.worker['id'], 1)[0]
        self.repo.complete(self.worker['id'], self.body(retry))
        with self.repo.transaction() as conn:
            conn.execute("UPDATE trademarks SET next_fetch_at=now()-interval '1 day'")
        self.repo.dispatch()
        self.assertEqual(self.repo.list('jobs')['total'], 3)

    def test_cancelled_reservation_archives_late_result(self):
        job = self.reserve()
        self.repo.edit_job(job['id'], {'action': 'cancel'})
        self.assertEqual(self.repo.complete(self.worker['id'], self.body(job))['disposition'], 'obsolete')
        self.repo.dispatch()
        self.assertEqual(self.repo.list('jobs')['total'], 1)

    def test_draining_worker_uploads_saved_work_without_claiming_more(self):
        self.repo.enqueue('1234567')
        engine, client = self.engine()
        client.fail_completion = True
        self.assertFalse(engine.cycle())
        self.repo.update_worker(self.worker['id'], {'status': 'draining', 'capacity': 5})
        self.repo.enqueue('7777777')
        client.fail_completion = False
        self.assertTrue(engine.cycle())
        self.assertEqual(self.repo.list('results')['total'], 1)
        self.assertEqual(engine.phase, 'paused')

    def test_crash_checkpoint_is_delivered_as_partial(self):
        self.repo.enqueue('1234567')
        engine, _ = self.engine()
        store = engine.store
        class InterruptedScraper:
            def scrape(inner, job):
                result = self.body(job)
                result['completeness'] = 'partial'
                result['warnings'] = ['Interrupted panel processing']
                store.save_result(job['id'] + ':' + job['reservation_token'], result, {})
                raise RuntimeError('Simulated interruption')
        engine.scraper = InterruptedScraper()
        self.assertTrue(engine.cycle())
        self.assertEqual(self.repo.list('results')['items'][0]['disposition'], 'partial')
        self.assertEqual(self.repo.list('jobs')['items'][0]['state'], 'retry_wait')

    def test_worker_ui_requires_auth_and_rejects_cross_origin_controls(self):
        from worker_agent.app import create_app as worker_app
        engine, _ = self.engine()
        client = worker_app(engine.settings, engine).test_client()
        self.assertEqual(client.get('/api/worker/status').status_code, 401)
        headers = {'Authorization': 'Bearer ' + engine.settings.local_token, 'Origin': 'https://unrelated.invalid'}
        self.assertEqual(client.post('/api/worker/pause', headers=headers).status_code, 403)

    def test_registered_artifact_missing_from_disk_blocks_completion(self):
        body = self.body(self.reserve(), [{'sha256': digest(b'pdf'), 'size': 3, 'filename': 'test.pdf', 'kind': 'document'}])
        self.repo.record_artifact(self.worker['id'], body['result_id'], digest(b'pdf'), 3)
        with self.assertRaises(Conflict):
            self.repo.complete(self.worker['id'], body)

    def test_offline_worker_is_not_reported_as_unreachable(self):
        job = self.reserve()
        self.repo.acknowledge_offline(self.worker['id'], [job])
        self.assertEqual(self.repo.overview()['worker_connectivity']['expected_offline'], 1)

    def test_failed_vpn_disconnect_prevents_coordinator_contact(self):
        engine, client = self.engine()
        class StuckVPN:
            active = True
            def disconnect(inner):
                raise RuntimeError('Simulated stuck tunnel')
        engine.vpn = StuckVPN()
        self.assertFalse(engine.cycle())
        self.assertEqual(self.repo.list('jobs')['total'], 0)

    def test_wireguard_state_query_fails_closed(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from worker_agent.engine import WireGuardVPN
        tunnel = WireGuardVPN('fixture-tunnel')
        with patch('worker_agent.engine.subprocess.run', return_value=SimpleNamespace(returncode=5, stdout='Access denied')):
            with self.assertRaises(RuntimeError):
                _ = tunnel.active
        with patch('worker_agent.engine.subprocess.run', return_value=SimpleNamespace(returncode=1060, stdout='')):
            self.assertFalse(tunnel.active)
        with patch('worker_agent.engine.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='STATE : 4 RUNNING')):
            self.assertTrue(tunnel.active)


if __name__ == '__main__':
    unittest.main()
