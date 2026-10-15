from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from worker_agent.config import WorkerSettings
from worker_agent.engine import WorkerEngine


class FakeVPN:
    def __init__(self, name, broken=()):
        self.name, self.broken, self.connected, self.active = name, set(broken), [], False

    def connect(self):
        if self.name in self.broken:
            raise RuntimeError('WireGuard tunnel did not reach the running state.')
        self.connected.append(self.name)
        self.active = True

    def disconnect(self):
        self.active = False


class FakeNetwork:
    def __init__(self):
        self.calls = []

    def preflight(self):
        self.calls.append('preflight')

    def invalidate(self):
        pass

    def portal_check(self):
        pass

    def report(self, **values):
        pass

    def snapshot(self):
        return {'effective_mode': 'offline'}


class VpnSwapTests(unittest.TestCase):
    def make(self, pool=('vpn-a', 'vpn-b', 'vpn-c'), broken=()):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = WorkerSettings('https://coordinator.test', 'worker-token-at-least-24-characters',
                                  'local-token-at-least-24-characters', Path(temp.name), mode='live',
                                  vpn_config='vpn-a', vpn_pool=tuple(pool))
        self.vpn = FakeVPN('vpn-a', broken)
        self.network = FakeNetwork()
        return WorkerEngine(settings, client=MagicMock(), scraper=MagicMock(), vpn=self.vpn, network=self.network)

    def test_no_pool_keeps_the_single_configured_tunnel(self):
        engine = self.make(pool=())
        engine._connect_tunnel()
        self.assertEqual(self.vpn.connected, ['vpn-a'])
        self.assertIsNone(engine._select_tunnel())

    def test_failed_tunnel_is_benched_and_the_next_one_is_used(self):
        engine = self.make(broken=('vpn-a',))
        engine._connect_tunnel()
        self.assertEqual(self.vpn.connected, ['vpn-b'])
        self.assertEqual(engine.settings.vpn_config, 'vpn-b')
        health = engine.store.get('vpn_health')
        self.assertEqual(health['vpn-a']['fails'], 1)
        self.assertGreater(health['vpn-a']['blocked_until'], time.time())
        self.assertNotEqual(engine._select_tunnel(), 'vpn-a')

    def test_all_tunnels_failing_raises_after_three_tries(self):
        engine = self.make(broken=('vpn-a', 'vpn-b', 'vpn-c'))
        with self.assertRaises(RuntimeError):
            engine._connect_tunnel()
        self.assertEqual(self.vpn.connected, [])

    def test_least_recently_used_healthy_tunnel_is_chosen_next(self):
        engine = self.make()
        engine._record_tunnel('vpn-a', True)
        time.sleep(0.01)
        engine._record_tunnel('vpn-b', True)
        self.assertEqual(engine._select_tunnel(), 'vpn-c')
        engine._record_tunnel('vpn-c', True)
        self.assertEqual(engine._select_tunnel(), 'vpn-a')

    def test_when_everything_is_benched_the_soonest_recovery_is_used(self):
        engine = self.make()
        for name in ('vpn-a', 'vpn-b', 'vpn-c'):
            engine._record_tunnel(name, False)
            time.sleep(0.01)
        self.assertEqual(engine._select_tunnel(), 'vpn-a')

    def test_pool_of_one_never_benches_its_only_tunnel(self):
        engine = self.make(pool=('vpn-a',))
        engine._record_tunnel('vpn-a', False)
        self.assertEqual(engine.store.get('vpn_health', {}), {})

    def test_unsafe_pool_names_are_rejected(self):
        with self.assertRaises(ValueError):
            WorkerSettings('https://coordinator.test', 'worker-token-at-least-24-characters',
                           'local-token-at-least-24-characters', Path('.'), vpn_pool=('../outside',))


if __name__ == '__main__':
    unittest.main()
