"""Verify the proposed service grants in the isolated localhost test DB."""
from dataclasses import replace
from pathlib import Path
import unittest
import uuid
from urllib.parse import quote, urlencode

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from control_plane.repository import Repository
from control_plane.contracts import digest
from migrations.prepare_legacy import prepare
from tests import test_fleet as fleet
from werkzeug.security import generate_password_hash
from worker_agent.scraping import make_result


class ServiceRoleTests(unittest.TestCase):
    setUpClass = classmethod(fleet.FleetTests.setUpClass.__func__)
    setUp = fleet.FleetTests.setUp

    def grant(self):
        prepare(self.url)
        with psycopg.connect(self.url) as conn:
            for role in ('tm_user_service', 'tm_control_service'):
                if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                    conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
            # Reset rehearsal grants so narrowed templates do not accidentally
            # pass by inheriting permissions from a previous test run.
            conn.execute('REVOKE ALL ON public.my_trademarks FROM tm_control_service')
            conn.execute((Path(__file__).parents[2] / 'deploy' / 'fleet-service-grants.sql').read_text())

    def role_url(self, role):
        return self.url + ('&' if '?' in self.url else '?') + urlencode({'options': '-c role=' + role}, quote_via=quote)

    def test_control_service_cannot_read_private_user_data(self):
        self.grant()
        with psycopg.connect(self.role_url('tm_control_service')) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT password_hash FROM users')
        with psycopg.connect(self.role_url('tm_user_service')) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT encrypted_credentials FROM fleet_control.accounts')
        with psycopg.connect(self.role_url('tm_control_service')) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE my_trademarks SET notes='modified' WHERE false")
        with psycopg.connect(self.role_url('tm_control_service')) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT notes FROM my_trademarks')
        with psycopg.connect(self.role_url('tm_control_service')) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT receipt_date FROM fleet_event_preferences')
        with psycopg.connect(self.role_url('tm_control_service')) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT user_id FROM fleet_proceeding_memberships')
        with psycopg.connect(self.role_url('tm_user_service')) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('UPDATE fleet_control.compliance_events SET needs_review=false WHERE false')

    def test_control_publication_with_column_scoped_portfolio_grants(self):
        self.grant()
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            user = conn.execute('INSERT INTO users(email,password_hash) VALUES(%s,%s) RETURNING id',
                                ('role-fixture@example.invalid', generate_password_hash('fixture-password'))).fetchone()
            self.addCleanup(self.remove_user, user['id'])
            conn.execute('INSERT INTO my_trademarks(user_id,app_no,notes) VALUES(%s,%s,%s)', (user['id'], '1122334', 'Private note stays intact'))
        repo = Repository(replace(self.settings, database_url=self.role_url('tm_control_service'), publish_legacy=True))
        repo.enqueue('1122334')
        job = repo.claim(self.worker['id'], 1)[0]
        self.assertEqual(repo.complete(self.worker['id'], fleet.FleetTests.body(self, job))['disposition'], 'accepted')
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            row = conn.execute('SELECT status,notes FROM my_trademarks WHERE user_id=%s', (user['id'],)).fetchone()
        self.assertEqual(row['status'], 'fetched')
        self.assertEqual(row['notes'], 'Private note stays intact')

    def remove_user(self, user_id):
        with psycopg.connect(self.url) as conn:
            conn.execute('DELETE FROM my_trademarks WHERE user_id=%s', (user_id,))
            conn.execute('DELETE FROM users WHERE id=%s', (user_id,))

    def test_notice_metadata_and_row_identity_survive_repeated_publication(self):
        self.grant()
        repo = Repository(replace(self.settings, database_url=self.role_url('tm_control_service'), publish_legacy=True))
        content = b'<html><body>Synthetic registry notice</body></html>'
        artifact = {'sha256': digest(content), 'size': len(content), 'filename': 'fixture-correspondence.html', 'kind': 'correspondence',
                    'metadata': {'s_no': '1', 'corres_no': 'fleet-fixture-8000001', 'corres_date': '01/10/2026',
                                 'subject': 'Fixture notice', 'despatch_no': '7000001', 'despatch_date': '02/10/2026',
                                 'view_url': '/estatus/RegisteredTM/ShowCorrespondence?CRS_NO=fixture-token'}}
        prior_id = None
        for _ in range(2):
            repo.enqueue('9988776')
            job = repo.claim(self.worker['id'], 1)[0]
            body = make_result(job, str(uuid.uuid4()), {'Class': '35', 'Application Date': '01/01/2026'}, [artifact], [], [], 'complete')
            path = self.settings.artifact_dir / self.worker['id'] / body['result_id'] / artifact['sha256']
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            repo.record_artifact(self.worker['id'], body['result_id'], artifact['sha256'], len(content))
            self.assertEqual(repo.complete(self.worker['id'], body)['disposition'], 'accepted')
            with psycopg.connect(self.url, row_factory=dict_row) as conn:
                rows = conn.execute("SELECT * FROM ipindia_correspondence WHERE app_no='9988776' AND corres_no='fleet-fixture-8000001'").fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row['corres_date'], '01/10/2026')
            self.assertEqual(row['despatch_date'], '02/10/2026')
            self.assertEqual(row['view_url'], artifact['metadata']['view_url'])
            if prior_id:
                self.assertEqual(row['id'], prior_id)
            prior_id = row['id']
