import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from flask import Flask, g

path = Path(__file__).resolve().parents[1] / 'tm_manager/control_admin_proxy.py'
spec = importlib.util.spec_from_file_location('admin_proxy_test', path)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class AdminProxyTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(proxy.bp)
        self.admin, self.user = False, None
        @self.app.before_request
        def session_user():
            g.is_admin, g.user = self.admin, self.user
        self.client = self.app.test_client()
        self.environment = patch.dict(os.environ, {'TM_CONTROL_API_BASE': 'http://127.0.0.1:5510',
                    'TM_CONTROL_ADMIN_TOKEN': 'synthetic-private-control-secret'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_ordinary_users_and_ops_credentials_cannot_relay(self):
        with patch.object(proxy.requests, 'Session') as network:
            for admin, user in ((False, {'id': 1}), (True, None), (False, None)):
                self.admin, self.user = admin, user
                self.assertEqual(self.client.get('/api/control/v1/admin/overview').status_code, 403)
            network.assert_not_called()

    def test_mutation_requires_custom_header_and_matching_origin(self):
        self.admin, self.user = True, {'id': 1}
        with patch.object(proxy.requests, 'Session') as network:
            for headers in ({}, {'X-Fleet-Request': '1', 'Origin': 'https://other.invalid'}):
                self.assertEqual(self.client.post('/api/control/v1/admin/jobs', json={}, headers=headers).status_code, 403)
            network.assert_not_called()

    def test_service_secret_stays_on_server_and_proxies_are_disabled(self):
        self.admin, self.user = True, {'id': 1}
        response = MagicMock(status_code=200, headers={'Content-Type': 'application/json', 'Set-Cookie': 'do-not-forward'})
        response.iter_content.return_value = [b'{"ok":true}']
        with patch.object(proxy.requests, 'Session') as network:
            session = network.return_value.__enter__.return_value
            session.request.return_value.__enter__.return_value = response
            reply = self.client.get('/api/control/v1/admin/overview', headers={'Authorization': 'Bearer browser-value'})
            self.assertEqual(reply.status_code, 200)
            self.assertFalse(session.trust_env)
            self.assertEqual(session.request.call_args.kwargs['headers']['Authorization'], 'Bearer synthetic-private-control-secret')
            self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
            self.assertNotIn('synthetic-private-control-secret', reply.get_data(as_text=True))
            self.assertNotIn('Set-Cookie', reply.headers)

    def test_remote_coordinator_configuration_is_refused(self):
        self.admin, self.user = True, {'id': 1}
        with patch.dict(os.environ, {'TM_CONTROL_API_BASE': 'https://other.invalid'}), patch.object(proxy.requests, 'Session') as network:
            self.assertEqual(self.client.get('/api/control/v1/admin/overview').status_code, 503)
            network.assert_not_called()

    def test_network_exception_does_not_expose_credentials(self):
        self.admin, self.user = True, {'id': 1}
        with patch.object(proxy.requests, 'Session') as network:
            network.return_value.__enter__.return_value.request.side_effect = proxy.requests.ConnectionError('synthetic-private-control-secret')
            reply = self.client.get('/api/control/v1/admin/overview')
            self.assertEqual(reply.status_code, 503)
            self.assertNotIn('synthetic-private-control-secret', reply.get_data(as_text=True))
