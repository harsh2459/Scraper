import logging
from dataclasses import replace
import queue
import shutil
import re
import subprocess
import threading
import time
from pathlib import Path

from control_plane.contracts import digest, timestamp, utcnow
from .scraping import FixtureScraper, PortalScraper
from .store import LocalStore
from .transport import CoordinatorClient
from .network import FixtureNetwork, WorkerNetwork, PortalRouteError


class FixtureVPN:
    """Simulates the phase boundary without modifying networking."""
    active = False

    def connect(self):
        self.active = True

    def disconnect(self):
        self.active = False


class WireGuardVPN:
    def __init__(self, name, config_dir=None):
        self.name = name
        self.config_dir = config_dir

    @property
    def active(self):
        state = self._state()
        return state is not None and state != 1

    def _state(self):
        # Query the owned tunnel's actual service state. A failed query cannot
        # prove the normal route is restored, so synchronization fails closed.
        reply = subprocess.run(['sc.exe', 'query', 'WireGuardTunnel$' + self.name],
                               capture_output=True, text=True, timeout=5)
        if reply.returncode == 1060:  # Service is not installed.
            return None
        match = re.search(r'STATE\s*:\s*(\d+)', reply.stdout)
        if reply.returncode or not match:
            raise RuntimeError('Unable to verify the configured WireGuard tunnel state.')
        return int(match.group(1))

    def connect(self):
        import vpn_rotator
        directory = Path(self.config_dir or vpn_rotator.CONFIG_DIR)
        if not (directory / (self.name + '.conf')).is_file():
            raise RuntimeError('Configured WireGuard tunnel was not found.')
        if not vpn_rotator.connect(self.name, config_dir=self.config_dir):
            raise RuntimeError('WireGuard connection failed; run worker with tunnel service privileges.')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._state() == 4:  # RUNNING
                return
            time.sleep(0.5)
        raise RuntimeError('WireGuard tunnel did not reach the running state.')

    def disconnect(self):
        import vpn_rotator
        # Explicitly clean only this worker's configured tunnel after a crash.
        if self._state() is not None:
            vpn_rotator.disconnect(self.name)
        # The service stops asynchronously (STOP_PENDING, then removed); wait for
        # it instead of treating the first query as final.
        deadline = time.monotonic() + 30
        while self.active and time.monotonic() < deadline:
            time.sleep(1)
        if self.active:
            raise RuntimeError('VPN did not disconnect; postponing coordinator delivery.')


class WorkerEngine:
    def __init__(self, settings, client=None, scraper=None, vpn=None, network=None):
        self.settings = settings
        self.store = LocalStore(settings.storage_dir)
        self.client = client or CoordinatorClient(settings)
        self.network = network or (FixtureNetwork(settings) if settings.mode == 'fixture' else WorkerNetwork(settings))
        self.scraper = scraper or (FixtureScraper(self.store) if settings.mode == 'fixture'
                                   else PortalScraper(self.store, settings.account_file, settings.full_check_days, self.network))
        self.vpn = vpn or (FixtureVPN() if settings.mode == 'fixture' else WireGuardVPN(settings.vpn_config, settings.vpn_config_dir))
        self.lock = threading.Lock()
        self.phase = 'idle'
        self.last_error = None
        self.io_lock = threading.RLock()
        self.monitor = None
        self.portal_blocked = threading.Event()
        self.batch_stopped = threading.Event()
        self._tunnel_risk = None
        self._pool_cleaned = False
        self.extra_scrapers = []
        if isinstance(self.client, CoordinatorClient):
            self.client.before_request = self._transport_guard

    # --- Automatic VPN swap -------------------------------------------------
    # With WORKER_VPN_POOL set to two or more tunnels, each batch uses the least
    # recently used healthy tunnel. A tunnel that fails to start, or whose batch
    # ends in a login/scrape failure, is benched with a growing cooldown and the
    # next one is used. Without a pool the single WORKER_VPN_CONFIG is used as before.
    def _pool(self):
        pool = list(self.settings.vpn_pool)
        return pool if len(pool) >= 2 else []

    def _select_tunnel(self, exclude=()):
        pool = [n for n in self._pool() if n not in exclude]
        if not pool:
            return None
        health, now = self.store.get('vpn_health', {}), time.time()
        usable = [n for n in pool if health.get(n, {}).get('blocked_until', 0) <= now]
        if usable:
            return min(usable, key=lambda n: health.get(n, {}).get('last_used', 0))
        # Everything is cooling down: use the one that recovers soonest rather than stall.
        return min(pool, key=lambda n: health.get(n, {}).get('blocked_until', 0))

    def _apply_tunnel(self, name):
        if name == self.settings.vpn_config:
            return
        self.settings = replace(self.settings, vpn_config=name)
        if hasattr(self.vpn, 'name'):
            self.vpn.name = name
        if hasattr(self.network, 'path'):
            directory = self.settings.vpn_config_dir or Path(__file__).resolve().parents[1] / 'vpn-configs'
            self.network.settings = self.settings
            self.network.path = directory / (name + '.conf')
            self.network.signature = None
            self.network.invalidate()
        self.store.event('VPN tunnel selected: ' + name)

    def _record_tunnel(self, name, ok):
        if not self._pool():
            return
        health, now = self.store.get('vpn_health', {}), time.time()
        entry = health.setdefault(name, {})
        if ok:
            entry.update(fails=0, blocked_until=0, last_used=now)
        else:
            fails = entry.get('fails', 0) + 1
            entry.update(fails=fails, blocked_until=now + min(10800, 300 * 2 ** (fails - 1)), last_used=now)
            self.store.event(f'VPN tunnel {name} benched after {fails} failure(s) in a row')
        self.store.set('vpn_health', health)

    def _cleanup_pool(self):
        # After a crash any pool tunnel may still be installed; remove them all once per process.
        if not hasattr(self.vpn, 'name'):
            return
        current = self.vpn.name
        try:
            for name in self._pool():
                self.vpn.name = name
                self.vpn.disconnect()
        finally:
            self.vpn.name = current
        self._pool_cleaned = True

    def _connect_tunnel(self):
        if not self._pool():
            self.network.invalidate()
            self.vpn.connect()
            self.network.portal_check()
            self._tunnel_risk = self.settings.vpn_config
            return
        tried, error = set(), None
        for _ in range(min(3, len(self._pool()))):
            name = self.settings.vpn_config
            try:
                self.network.preflight()
                self.network.invalidate()
                self.vpn.connect()
                self.network.portal_check()
                self._tunnel_risk = name
                return
            except Exception as exc:
                error = exc
                logging.getLogger('worker_agent').exception('VPN tunnel %s unusable; trying another', name)
                self._record_tunnel(name, False)
                try:
                    self.vpn.disconnect()
                except Exception:
                    pass
                tried.add(name)
                following = self._select_tunnel(tried)
                if following is None:
                    break
                self._apply_tunnel(following)
        raise error

    # --- Parallel portal sessions ------------------------------------------
    # With WORKER_PARALLEL_ACCOUNTS=N and N accounts assigned to this worker, each account
    # gets its own login session and its own thread; they share one VPN tunnel and one job
    # queue. With one account (the default) this runs exactly as the old sequential loop.
    def _all_scrapers(self):
        return [self.scraper] + self.extra_scrapers

    def _close_scrapers(self, stop_only=False):
        first = None
        for scraper in self._all_scrapers():
            # close() already stops a session, so a full close must not stop it a second time.
            for name in (('stop',) if stop_only else ('close',)):
                if hasattr(scraper, name):
                    try:
                        getattr(scraper, name)()
                    except Exception as exc:
                        first = first or exc
        if not stop_only:
            self.extra_scrapers = []
        if first:
            raise first

    # Seconds between the start of each account's login, so OTP requests do not all go out at once.
    LOGIN_STAGGER_SECONDS = 8

    def _prepare_scrapers(self, accounts):
        scrapers = [self.scraper]
        wanted = self.settings.parallel_accounts
        if wanted > 1 and self.settings.mode == 'live' and isinstance(self.scraper, PortalScraper):
            for account in accounts[1:wanted]:
                extra = PortalScraper(self.store, None, self.settings.full_check_days, self.network)
                extra.account = account
                self.extra_scrapers.append(extra)
            scrapers += self.extra_scrapers
            if len(scrapers) < wanted:
                self.store.event(f'Only {len(scrapers)} portal account(s) assigned; wanted {wanted}.')
        return scrapers

    def _scrape_items(self, scrapers, remaining, started):
        work = queue.Queue()
        for item in remaining:
            work.put(item)
        lock = threading.Lock()
        outcome = {'fatal': None, 'retired': 0, 'logged_in': 0, 'login_error': None}

        def log_in(scraper, position):
            # Each session logs in on its own thread, so one slow OTP email only delays that
            # account; the others start scraping as soon as they are in.
            if not hasattr(scraper, 'start'):
                with lock:
                    outcome['logged_in'] += 1
                return True
            if position:
                time.sleep(position * self.LOGIN_STAGGER_SECONDS)
            try:
                scraper.start()
            except Exception as exc:
                if len(scrapers) == 1:
                    raise
                logging.getLogger('worker_agent').exception('Portal login failed for one account')
                with lock:
                    outcome['retired'] += 1
                    outcome['login_error'] = exc
                return False
            with lock:
                outcome['logged_in'] += 1
            return True

        def run(scraper, position=0):
            consecutive = 0
            try:
                if not log_in(scraper, position):
                    return
                while True:
                    if self.store.get('paused', False) or self.batch_stopped.is_set() or time.monotonic() - started >= self.settings.batch_seconds:
                        return
                    if consecutive >= 3:
                        # Likely a dead session for this account or a blocked VPN address. Stop it here
                        # so untouched jobs stay reserved instead of being failed.
                        self.store.event('Stopped a portal session after 3 consecutive failures; remaining work stays reserved.')
                        with lock:
                            outcome['retired'] += 1
                        return
                    try:
                        item = work.get_nowait()
                    except queue.Empty:
                        return
                    if (timestamp(item['assignment']['expires_at']) - utcnow()).total_seconds() <= 180:
                        return
                    if self.portal_blocked.is_set():
                        raise PortalRouteError('Portal routing failed; saved work will resume after recovery.')
                    self.network.portal_check()
                    self.store.transition(item['key'], 'scraping')
                    try:
                        result, paths = scraper.scrape(item['assignment'])
                        self.store.save_result(item['key'], result, paths)
                        consecutive = 0
                    except Exception as exc:
                        logging.getLogger('worker_agent').exception('Scrape failed for application %s', item['assignment'].get('app_no'))
                        # Do not put credentials or raw portal responses into logs.
                        state = 'upload_pending' if self.store.has_result(item['key']) else 'failure_pending'
                        if state == 'failure_pending':
                            consecutive += 1
                        self.store.transition(item['key'], state, error=type(exc).__name__ + ': portal processing failed')
                    self._online_delivery()
            except Exception as exc:
                with lock:
                    outcome['fatal'] = outcome['fatal'] or exc

        if len(scrapers) == 1:
            run(scrapers[0])
        else:
            threads = [threading.Thread(target=run, args=(s, n), daemon=True, name=f'scrape-{n}') for n, s in enumerate(scrapers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        if outcome['fatal']:
            raise outcome['fatal']
        if outcome['logged_in'] == 0 and outcome['login_error']:
            raise outcome['login_error']
        if outcome['logged_in'] < len(scrapers):
            self.store.event(f"{outcome['logged_in']} of {len(scrapers)} portal sessions logged in.")
        return outcome['retired'] >= len(scrapers) and not work.empty()

    def status(self):
        return {'phase': self.phase, 'paused': self.store.get('paused', False), 'mode': self.settings.mode,
                'last_error': self.last_error, 'counts': self.store.counts(),
                'free_bytes': shutil.disk_usage(self.settings.storage_dir).free,
                'coordinator_url': self.settings.coordinator_url, 'network': self.network.snapshot()}

    def _metadata(self):
        inventory = []
        if self.settings.mode == 'live':
            directory = self.settings.vpn_config_dir or Path(__file__).resolve().parents[1] / 'vpn-configs'
            for path in sorted(directory.glob('*.conf')):
                if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', path.stem):
                    inventory.append({'name': path.stem, 'selected': path.stem == self.settings.vpn_config})
        return {'counts': self.store.counts(), 'mode': self.settings.mode, 'network': self.network.snapshot(),
                'phase': self.phase, 'paused': self.store.get('paused', False),
                'free_bytes': shutil.disk_usage(self.settings.storage_dir).free,
                'last_error': self.last_error, 'vpn_inventory': inventory,
                'vpn_config_count': len(inventory), 'selected_vpn': self.settings.vpn_config}

    def _transport_guard(self):
        if self.vpn.active:
            if self.settings.network_mode != 'split':
                raise RuntimeError('Coordinator traffic is disabled during the offline VPN phase.')
            self.network.coordinator_check()

    def _online_tick(self):
        # A failed portal check stops scraping. Coordinator failure only queues
        # delivery; it cannot revoke the already persisted batch locally.
        try:
            self.network.portal_check()
        except PortalRouteError:
            self.portal_blocked.set()
            self.network.report(effective_mode='offline')
            return False
        with self.io_lock:
            try:
                self.network.coordinator_check()
                self.client.health()
                self.network.report(effective_mode='split', coordinator='simulated' if self.settings.mode == 'fixture' else 'verified', reason=None)
                reply = self.client.heartbeat('scraping', self._metadata())
                if reply.get('worker_status') != 'active':
                    self.batch_stopped.set()
                self.store.set('network', self.network.snapshot())
                return True
            except Exception:
                self.network.report(effective_mode='offline', coordinator='unreachable', reason='Coordinator unavailable; delivery retained locally.')
                self.store.set('network', self.network.snapshot())
                return False

    def _monitor_network(self, stop):
        while not stop.wait(self.settings.network_check_seconds):
            self._online_tick()
            if self.portal_blocked.is_set():
                return

    def _online_delivery(self):
        if self.settings.network_mode != 'split' or self.network.snapshot()['effective_mode'] != 'split':
            return
        with self.io_lock:
            try:
                self.network.coordinator_check()
                self.upload_pending()
            except Exception:
                self.network.report(effective_mode='offline', coordinator='unreachable', reason='Online delivery interrupted; saved results will be retried.')
                self.store.set('network', self.network.snapshot())

    def _phase(self, phase):
        self.phase = phase
        self.store.set('phase', phase)
        self.store.event('Phase: ' + phase)

    def upload_pending(self):
        for item in self.store.list(('upload_pending', 'failure_pending'), limit=self.settings.max_pending):
            if item['state'] == 'failure_pending':
                response = self.client.fail(item['assignment'], item['error'])
                self.store.transition(item['key'], 'acknowledged', disposition=response['disposition'])
                continue
            result = item['result']
            for artifact in result['artifacts']:
                content = Path(item['paths'][artifact['sha256']]).read_bytes()
                if digest(content) != artifact['sha256']:
                    raise RuntimeError('Local artifact checksum failed; saved result requires operator recovery.')
                self.client.upload(result, artifact, content)
            response = self.client.complete(result)
            if response.get('acknowledged') is not True:
                raise RuntimeError('Coordinator did not acknowledge result storage.')
            self.store.transition(item['key'], 'acknowledged', disposition=response['disposition'])
            self.store.event(f"Result {result['result_id']}: {response['disposition']}")

    def _upload_after_vpn(self, attempts=4, delay=10):
        # DNS and routes can take a few seconds to settle after the tunnel closes.
        for attempt in range(1, attempts + 1):
            try:
                self.network.invalidate()
                return self.upload_pending()
            except Exception:
                logging.getLogger('worker_agent').exception('Upload after VPN teardown failed (attempt %d/%d)', attempt, attempts)
                if attempt == attempts:
                    raise
                time.sleep(delay)

    def cycle(self):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self.last_error = None
            if self.monitor and self.monitor.is_alive():
                raise RuntimeError('Previous connectivity monitor is still stopping; postponing a new cycle.')
            self.portal_blocked.clear()
            self.batch_stopped.clear()
            # Recovery starts by restoring the normal route, including after
            # reboot with a tunnel left installed. Online mode is verified
            # afresh only after this recovery and a new bounded batch.
            self._phase('syncing')
            self._close_scrapers()
            self.vpn.disconnect()
            self._tunnel_risk = None
            if self._pool():
                if not self._pool_cleaned:
                    self._cleanup_pool()
                following = self._select_tunnel()
                if following:
                    self._apply_tunnel(following)
            self.network.invalidate()
            self.network.report(effective_mode='offline')
            self.upload_pending()
            reply = self.client.heartbeat('idle', self._metadata())
            self.network.report(coordinator='simulated' if self.settings.mode == 'fixture' else 'verified', reason=None)
            active = self.client.reconcile()
            valid = {(j['id'], j['reservation_token']) for j in active}
            for item in self.store.list(('received',), limit=self.settings.max_pending):
                if (item['job_id'], item['token']) not in valid:
                    self.store.transition(item['key'], 'obsolete', disposition='reservation_lost')
            if reply.get('worker_status') == 'pending':
                if self.store.get('announced_pending') is not True:
                    self.store.event('Enrolled. Waiting for an administrator to name and approve this machine.')
                    self.store.set('announced_pending', True)
                self._phase('paused')
                return True
            if self.store.get('paused', False) or reply.get('worker_status') != 'active':
                self._phase('paused')
                return True
            if shutil.disk_usage(self.settings.storage_dir).free < self.settings.min_free_bytes:
                raise RuntimeError('Low local disk space; new assignments paused.')
            pending = self.store.counts().get('upload_pending', 0)
            if pending >= self.settings.max_pending:
                raise RuntimeError('Local delivery backlog reached its limit.')
            # Saved PDF repair is independent of portal DNS/VPN availability.
            # Its restricted claim can never reserve a new portal refresh.
            jobs = self.client.claim(self.settings.batch_size, repair_only=True)
            if any(not job.get('repair_result_id') for job in jobs):
                raise RuntimeError('Coordinator does not support repair-only claims; update it before this worker.')
            if not jobs:
                # Local config/DNS failures leave portal jobs queued with zero
                # new attempts, rather than expiring an idle reservation.
                self.network.preflight()
                jobs = self.client.claim(self.settings.batch_size)
            self.store.receive(jobs)
            items = self.store.list(('received',), limit=self.settings.batch_size)
            if jobs and all(job.get('repair_result_id') for job in jobs):
                items = [item for item in items if item['assignment'].get('repair_result_id')]
            if not items:
                self._phase('idle')
                return True
            remaining = []
            for item in items:
                expiry = timestamp(item['assignment']['expires_at'])
                if (expiry - utcnow()).total_seconds() <= 180:
                    self.store.transition(item['key'], 'failure_pending', error='Reservation too short to safely begin scraping')
                else:
                    remaining.append(item)
            if not remaining:
                self.upload_pending()
                self._phase('idle')
                return True
            for item in [i for i in remaining if i['assignment'].get('repair_result_id')]:
                self._phase('extracting')
                self.store.transition(item['key'], 'scraping')
                try:
                    from .reextract import reextract
                    result, paths = reextract(self.store, item['assignment'])
                    self.store.save_result(item['key'], result, paths)
                except Exception:
                    self.store.transition(item['key'], 'failure_pending', error='Saved artifact extraction failed; refresh or restore local files')
            remaining = [i for i in remaining if not i['assignment'].get('repair_result_id')]
            if not remaining:
                self.upload_pending()
                self._phase('idle')
                return True
            # Fetch credentials only after reserving work. Account reassignment
            # rejects workers holding reservations, so this snapshot cannot be
            # moved to another machine before the offline batch finishes.
            accounts = []
            if self.settings.mode == 'live':
                configuration = self.client.configuration()
                accounts = configuration.get('accounts', [])
                if accounts:
                    self.scraper.account = accounts[0]
                elif not self.settings.account_file:
                    raise RuntimeError('No portal account is assigned to this worker.')
            self.client.offline([item['assignment'] for item in remaining])
            self._phase('scraping')
            started = time.monotonic()
            monitor_stop = threading.Event()
            try:
                self._connect_tunnel()
                if self.settings.network_mode == 'split':
                    self._online_tick()
                    self.monitor = threading.Thread(target=self._monitor_network, args=(monitor_stop,), daemon=True)
                    self.monitor.start()
                scrapers = self._prepare_scrapers(accounts)
                tripped = self._scrape_items(scrapers, remaining, started)
                if tripped:
                    self._record_tunnel(self.settings.vpn_config, False)
                else:
                    self._record_tunnel(self.settings.vpn_config, True)
            finally:
                monitor_stop.set()
                try:
                    self._close_scrapers(stop_only=True)
                except Exception:
                    pass
                if self.monitor:
                    self.monitor.join(timeout=45)
                    if self.monitor.is_alive():
                        raise RuntimeError('Connectivity monitor did not stop; postponing tunnel teardown.')
                try:
                    self._close_scrapers()
                finally:
                    self.vpn.disconnect()
                    self.network.invalidate()
                    self.network.report(effective_mode='offline')
            self._tunnel_risk = None
            self._phase('upload_pending')
            self._upload_after_vpn()
            self.client.heartbeat('idle', self._metadata())
            self._phase('idle')
            return True
        except Exception as exc:
            # Traceback goes to the local log only; the UI/event text stays generic.
            logging.getLogger('worker_agent').exception('Cycle interrupted')
            self.last_error = type(exc).__name__ + ': cycle interrupted; saved work will be retried'
            self.store.event(self.last_error)
            if self._tunnel_risk:
                # The failure happened while this tunnel was in use: bench it so the next cycle swaps.
                self._record_tunnel(self._tunnel_risk, False)
                self._tunnel_risk = None
            self._phase('error')
            try:
                # Report preflight/delivery failures only after verified tunnel
                # cleanup. Never contact Contabo to report a cleanup failure.
                if not self.vpn.active:
                    self.client.heartbeat('error', self._metadata())
            except Exception:
                pass
            return False
        finally:
            self.lock.release()

    def background(self, stop):
        delay = 15
        while not stop.is_set():
            success = self.cycle()
            delay = 15 if success else min(300, delay * 2)
            stop.wait(delay)
