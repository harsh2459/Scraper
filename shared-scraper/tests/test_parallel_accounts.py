from datetime import timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from control_plane.contracts import utcnow
from worker_agent.config import WorkerSettings
from worker_agent.engine import WorkerEngine
from worker_agent.scraping import PortalScraper


class Network:
    def portal_check(self, url=None):
        pass

    def invalidate(self):
        pass

    def report(self, **values):
        pass

    def snapshot(self):
        return {'effective_mode': 'offline'}


class GoodScraper:
    delay = 0.2

    def __init__(self):
        self.threads, self.apps = set(), []

    def scrape(self, assignment):
        self.threads.add(threading.current_thread().name)
        time.sleep(self.delay)
        self.apps.append(assignment['app_no'])
        return {'result_id': 'r-' + assignment['app_no']}, {}


class LoginScraper(GoodScraper):
    """A session that must log in (start) before it can scrape."""
    def __init__(self, login_seconds, fail=False):
        super().__init__()
        self.login_seconds, self.fail, self.logged_in_at = login_seconds, fail, None

    def start(self):
        time.sleep(self.login_seconds)
        if self.fail:
            raise RuntimeError('OTP not received')
        self.logged_in_at = time.monotonic()


class BadScraper:
    def __init__(self):
        self.calls = 0

    def scrape(self, assignment):
        self.calls += 1
        raise RuntimeError('portal session lost')


class ParallelTests(unittest.TestCase):
    def make(self, accounts=3, mode='live'):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = WorkerSettings('https://coordinator.test', 'worker-token-at-least-24-characters',
                                  'local-token-at-least-24-characters', Path(temp.name), mode=mode,
                                  vpn_config='vpn-a', parallel_accounts=accounts, batch_size=20)
        engine = WorkerEngine(settings, client=MagicMock(), scraper=MagicMock(), vpn=MagicMock(), network=Network())
        return engine

    def load(self, engine, count):
        expires = (utcnow() + timedelta(hours=3)).isoformat()
        engine.store.receive([{'id': f'job-{i}', 'reservation_token': 't', 'app_no': str(1000 + i), 'expires_at': expires,
                               'generation': 1} for i in range(count)])
        return engine.store.list(('received',), limit=100)

    def test_three_sessions_work_at_once_and_each_job_runs_exactly_once(self):
        engine = self.make()
        scrapers = [GoodScraper(), GoodScraper(), GoodScraper()]
        items = self.load(engine, 9)
        began = time.monotonic()
        tripped = engine._scrape_items(scrapers, items, time.monotonic())
        elapsed = time.monotonic() - began
        self.assertFalse(tripped)
        self.assertLess(elapsed, 1.2)  # sequential would take 9 * 0.2 = 1.8 s
        done = [app for s in scrapers for app in s.apps]
        self.assertEqual(sorted(done), sorted(i['assignment']['app_no'] for i in items))
        self.assertEqual(len(set(done)), 9)
        self.assertTrue(all(s.apps for s in scrapers))
        self.assertEqual(engine.store.counts(), {'upload_pending': 9})

    def test_one_account_behaves_like_the_old_sequential_loop(self):
        engine = self.make(accounts=1)
        scraper = GoodScraper()
        scraper.delay = 0
        items = self.load(engine, 4)
        self.assertFalse(engine._scrape_items([scraper], items, time.monotonic()))
        self.assertEqual(scraper.apps, [i['assignment']['app_no'] for i in items])

    def test_a_dying_session_stops_alone_and_the_others_finish_the_queue(self):
        engine = self.make()
        bad, good = BadScraper(), GoodScraper()
        good.delay = 0.05
        items = self.load(engine, 12)
        tripped = engine._scrape_items([bad, good], items, time.monotonic())
        self.assertFalse(tripped)
        self.assertEqual(bad.calls, 3)
        counts = engine.store.counts()
        self.assertEqual(counts.get('failure_pending'), 3)
        self.assertEqual(counts.get('upload_pending'), 9)
        self.assertNotIn('received', counts)

    def test_all_sessions_failing_stops_the_batch_and_leaves_the_rest_reserved(self):
        engine = self.make()
        scrapers = [BadScraper(), BadScraper()]
        items = self.load(engine, 20)
        tripped = engine._scrape_items(scrapers, items, time.monotonic())
        self.assertTrue(tripped)
        counts = engine.store.counts()
        self.assertEqual(counts.get('failure_pending'), 6)
        self.assertEqual(counts.get('received'), 14)

    def test_only_the_configured_number_of_accounts_gets_a_session(self):
        engine = self.make(accounts=3)
        engine.scraper = PortalScraper(engine.store, None, 30, engine.network)
        accounts = [{'email_address': f'a{i}@example.test'} for i in range(4)]
        engine.scraper.account = accounts[0]
        scrapers = engine._prepare_scrapers(accounts)
        self.assertEqual([s.account['email_address'] for s in scrapers], ['a0@example.test', 'a1@example.test', 'a2@example.test'])

    def test_a_slow_login_does_not_hold_up_the_sessions_that_are_ready(self):
        engine = self.make()
        engine.LOGIN_STAGGER_SECONDS = 0
        fast, slow = LoginScraper(0), LoginScraper(1.0)
        fast.delay = 0.05
        items = self.load(engine, 6)
        began = time.monotonic()
        self.assertFalse(engine._scrape_items([fast, slow], items, began))
        self.assertEqual(len(fast.apps), 6)   # the ready session did all of it while the other was still logging in
        self.assertEqual(slow.apps, [])
        self.assertEqual(engine.store.counts(), {'upload_pending': 6})

    def test_logins_are_staggered(self):
        engine = self.make()
        engine.LOGIN_STAGGER_SECONDS = 0.3
        first, second = LoginScraper(0), LoginScraper(0)
        first.delay = second.delay = 0
        engine._scrape_items([first, second], self.load(engine, 2), time.monotonic())
        self.assertGreaterEqual(second.logged_in_at - first.logged_in_at, 0.25)

    def test_a_failed_login_is_skipped_and_the_others_finish_the_queue(self):
        engine = self.make()
        engine.LOGIN_STAGGER_SECONDS = 0
        broken, good = LoginScraper(0, fail=True), LoginScraper(0)
        good.delay = 0.01
        items = self.load(engine, 5)
        self.assertFalse(engine._scrape_items([good, broken, LoginScraper(0)], items, time.monotonic()))
        self.assertEqual(engine.store.counts(), {'upload_pending': 5})

    def test_login_failure_for_every_account_raises(self):
        engine = self.make(accounts=2)
        engine.LOGIN_STAGGER_SECONDS = 0
        items = self.load(engine, 3)
        with self.assertRaises(RuntimeError):
            engine._scrape_items([LoginScraper(0, fail=True), LoginScraper(0, fail=True)], items, time.monotonic())
        self.assertEqual(engine.store.counts(), {'received': 3})

    def test_single_account_login_failure_raises_like_before(self):
        engine = self.make(accounts=1)
        items = self.load(engine, 2)
        with self.assertRaises(RuntimeError):
            engine._scrape_items([LoginScraper(0, fail=True)], items, time.monotonic())

    def test_a_dead_login_plus_a_dying_session_counts_as_a_failed_batch(self):
        engine = self.make(accounts=2)
        engine.LOGIN_STAGGER_SECONDS = 0
        dying = LoginScraper(0)
        dying.scrape = BadScraper().scrape
        items = self.load(engine, 10)
        tripped = engine._scrape_items([dying, LoginScraper(0, fail=True)], items, time.monotonic())
        self.assertTrue(tripped)
        self.assertEqual(engine.store.counts().get('received'), 7)

    def test_parallel_limit_is_validated(self):
        for bad in (0, 9):
            with self.assertRaises(ValueError):
                WorkerSettings('https://coordinator.test', 'worker-token-at-least-24-characters',
                               'local-token-at-least-24-characters', Path('.'), parallel_accounts=bad)


if __name__ == '__main__':
    unittest.main()
