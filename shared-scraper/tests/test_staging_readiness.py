import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

dependency = types.ModuleType('prepare_server_staging')
dependency.STATE = Path('/synthetic/staging-state.json')
dependency.read_env = lambda path: {}
spec = importlib.util.spec_from_file_location('staging_readiness', Path(__file__).resolve().parents[2] / 'deploy/containers/check_server_staging.py')
check = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'prepare_server_staging': dependency}):
    spec.loader.exec_module(check)


class Opener:
    def __init__(self, code, body):
        self.code, self.body = code, body
    def open(self, url, **kwargs):
        if self.code != 200:
            raise HTTPError(url, self.code, 'synthetic', {}, io.BytesIO(self.body))
        return io.BytesIO(self.body)


class ReadinessTests(unittest.TestCase):
    def test_login_required_response_is_ready(self):
        check.check_user_api(Opener(401, b'{"error":"auth required","login":"/login"}'))
        check.check_user_api(Opener(200, b'{"auth":false}'))

    def test_other_errors_and_unexpected_authenticated_response_fail(self):
        for code, body in ((401, b'{"error":"other failure"}'), (503, b'{}'),
                           (200, b'{"auth":true}'), (200, b'not json')):
            with self.assertRaises((ValueError, HTTPError)):
                check.check_user_api(Opener(code, body))
