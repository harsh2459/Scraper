import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from worker_agent import enrollment
from worker_agent.config import WorkerSettings
from worker_agent.enrollment import EnrollmentError, resolve_token

LOCAL_TOKEN = 'local-token-at-least-24-characters'


class FakeCoordinator:
    """Minimal stand-in for POST /api/control/v1/enroll."""
    def __init__(self, status=201):
        self.status, self.calls = status, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                outer.calls.append((self.path, body))
                payload = ({'worker_id': 'w-1', 'token': 'issued-token-' + 'x' * 30} if outer.status == 201 else {'error': 'no'})
                raw = json.dumps(payload).encode()
                self.send_response(outer.status); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.addCleanup(lambda: self.server.close() if getattr(self, 'server', None) else None)

    def serve(self, status=201):
        self.server = FakeCoordinator(status)
        return self.server

    def test_an_explicit_token_is_used_and_nothing_is_contacted(self):
        server = self.serve()
        token = resolve_token({'WORKER_TOKEN': 'explicit-token-' + 'y' * 20, 'WORKER_ENROLL_KEY': 'tmk_k'}, server.url, self.dir)
        self.assertEqual(token, 'explicit-token-' + 'y' * 20)
        self.assertEqual(server.calls, [])

    def test_first_start_enrolls_and_later_starts_reuse_the_saved_credential(self):
        server = self.serve()
        env = {'WORKER_ENROLL_KEY': 'tmk_secret', 'WORKER_MACHINE_NAME': 'Office PC 9'}
        first = resolve_token(env, server.url, self.dir)
        self.assertTrue(first.startswith('issued-token-'))
        self.assertEqual(server.calls, [('/api/control/v1/enroll', {'key': 'tmk_secret', 'hostname': 'Office PC 9'})])
        again = resolve_token({'WORKER_ENROLL_KEY': 'tmk_secret'}, server.url, self.dir)  # restart
        self.assertEqual(again, first)
        self.assertEqual(len(server.calls), 1)  # no second enrollment
        saved = json.loads((self.dir / 'worker_identity.json').read_text())
        self.assertEqual((saved['worker_id'], saved['coordinator_url']), ('w-1', server.url))

    def test_a_credential_saved_for_another_coordinator_is_not_reused(self):
        server = self.serve()
        enrollment.save_identity(self.dir, {'coordinator_url': 'https://old.example', 'worker_id': 'old', 'token': 'o' * 40})
        token = resolve_token({'WORKER_ENROLL_KEY': 'tmk_secret'}, server.url, self.dir)
        self.assertTrue(token.startswith('issued-token-'))

    def test_a_refused_key_gives_a_clear_error_and_saves_nothing(self):
        for status, words in ((403, 'refused'), (429, 'Too many')):
            server = self.serve(status)
            with self.assertRaises(EnrollmentError) as caught:
                resolve_token({'WORKER_ENROLL_KEY': 'tmk_bad'}, server.url, self.dir)
            self.assertIn(words, str(caught.exception))
            server.close()
        self.assertFalse((self.dir / 'worker_identity.json').exists())

    def test_no_token_and_no_key_means_no_credential(self):
        self.assertEqual(resolve_token({}, 'http://127.0.0.1:1', self.dir), '')

    def test_unreachable_coordinator_is_reported_not_crashed(self):
        with patch('worker_agent.enrollment.time.sleep'):
            with self.assertRaises(EnrollmentError) as caught:
                resolve_token({'WORKER_ENROLL_KEY': 'tmk_k'}, 'http://127.0.0.1:1', self.dir)
        self.assertIn('Could not reach', str(caught.exception))

    def test_settings_can_start_from_an_enrollment_key_alone(self):
        server = self.serve()
        env = {'WORKER_COORDINATOR_URL': server.url, 'WORKER_ENROLL_KEY': 'tmk_secret', 'WORKER_LOCAL_TOKEN': LOCAL_TOKEN,
               'WORKER_STORAGE_DIR': str(self.dir)}
        with patch.dict(os.environ, env, clear=True):
            settings = WorkerSettings.from_env()
        self.assertTrue(settings.worker_token.startswith('issued-token-'))
        self.assertEqual(settings.storage_dir, self.dir.resolve())

    def test_settings_explain_what_is_missing(self):
        env = {'WORKER_COORDINATOR_URL': 'http://127.0.0.1:1', 'WORKER_LOCAL_TOKEN': LOCAL_TOKEN, 'WORKER_STORAGE_DIR': str(self.dir)}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError) as caught:
                WorkerSettings.from_env()
        self.assertIn('WORKER_ENROLL_KEY', str(caught.exception))

    def test_the_key_is_never_sent_over_plain_http_to_a_remote_coordinator(self):
        env = {'WORKER_COORDINATOR_URL': 'http://coordinator.example', 'WORKER_ENROLL_KEY': 'tmk_secret',
               'WORKER_LOCAL_TOKEN': LOCAL_TOKEN, 'WORKER_STORAGE_DIR': str(self.dir)}
        with patch.dict(os.environ, env, clear=True), patch('worker_agent.enrollment.requests.Session') as session:
            with self.assertRaises(ValueError):
                WorkerSettings.from_env()
        session.assert_not_called()


if __name__ == '__main__':
    unittest.main()
