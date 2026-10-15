"""
Scraper for the IP India Trade Marks Registry e-Status portal.
https://tmrsearch.ipindia.gov.in/estatus

Authentication flow (fully automated)
--------------------------------------
1. GET  /estatus/Otp/GetCaptcha  → math puzzle: "Enter the Last number in: 2 3 2 8"
                                    solve by extracting the last digit → 8
2. POST /estatus/Otp/SendOtp     → {"Email": "...", "CaptchaAnswer": 8}
                                    triggers OTP to email inbox
3. (email_otp reads OTP from inbox automatically)
4. POST /estatus/Otp/VerifyOtp   → {"Email": "...", "OtpCode": "123456"}
                                    returns {"success": true, "tabToken": "..."}
5. GET  /estatus/Home/Index      → authenticated home; scrape from here

NOTE: The CAPTCHA is trivially solvable — no paid 3rd-party service required.
"""

import logging
import os
import re
import threading
import time
import urllib.parse
from enum import Enum
from pathlib import Path
from typing import Optional
import base64

import pdfplumber
import requests
from bs4 import BeautifulSoup

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key

import email_otp
from email_otp import get_otp
import config
from portal_formats import parse_notice_table, portal_url

log = logging.getLogger(__name__)

BASE         = "https://tmrsearch.ipindia.gov.in/estatus"
CAPTCHA_URL  = f"{BASE}/Otp/GetCaptcha"
SEND_OTP_URL = f"{BASE}/Otp/SendOtp"
VERIFY_URL   = f"{BASE}/Otp/VerifyOtp"
KEEPALIVE_URL= f"{BASE}/Otp/keepalive"
HOME_URL     = f"{BASE}/Home/Index"
SEARCH_URL   = f"{BASE}/Home/Search"

_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
try:
    from fake_useragent import UserAgent as _UA

    _ua_gen = _UA(browsers=["chrome", "firefox", "edge"], os=["windows", "macos"], fallback=_UA_FALLBACK)

    def _random_ua() -> str:
        return _ua_gen.random
except Exception:
    # Library unavailable (not installed yet, or offline on first run with no
    # cached UA database) — fall back to one fixed string rather than fail.
    def _random_ua() -> str:
        return _UA_FALLBACK
_KEEPALIVE_INTERVAL = 55   # seconds (server expires at 60 s)

_SESSION_EXPIRED_MARKER = "session expired"  # exact lowercased substring IP
                                              # India's own portal uses in a
                                              # failed-request response body
                                              # (observed from TrackClick)
                                              # when tab_token is no longer
                                              # valid server-side.


# ── Portal-wide block circuit breaker ───────────────────────────────────────
#
# Observed 2026-09-23: IP India's portal returned HTTP 401 on GetCaptcha
# (and even the bare /estatus/ home page, before any login attempt) for
# EVERY pool account on this machine at once — not a per-account cooldown or
# a bad CAPTCHA answer, but the portal itself refusing this machine's
# requests outright, for roughly 15 minutes before clearing on its own.
#
# Before this breaker existed, that condition was indistinguishable from any
# other login failure: every account still got claimed, still burned a full
# retry budget, and still got marked "failed" in fleet_assignments — during
# one 401 episode this produced ~2400 failed rows and zero successful
# fetches, because nothing stopped the fleet's 6 workers from immediately
# reclaiming a fresh account and retrying the exact same doomed request.
#
# is_portal_blocked() lets callers check BEFORE even claiming a pool account
# (see app.py's _pool_claim_and_authenticate) — claiming and then
# immediately failing wastes the claim itself (another caller has to wait
# out that account's normal cooldown for nothing). record_portal_401() is
# called only when the failure is unambiguously "the portal itself rejected
# this request" (a 401 on the CAPTCHA fetch or the page load before it),
# never for account-specific failures (wrong OTP, bad CAPTCHA answer,
# session expired mid-search) — those still go through the existing
# per-account retry/cooldown machinery untouched.
_PORTAL_BLOCK_COOLDOWN_SECONDS = 20 * 60  # generous margin over the ~15 min observed
_portal_block_lock = threading.Lock()
_portal_blocked_until: float = 0.0


def is_portal_blocked() -> tuple[bool, float]:
    """(True, seconds_remaining) if the portal-wide breaker is currently
    tripped, else (False, 0.0). Cheap — safe to call before every login
    attempt."""
    with _portal_block_lock:
        remaining = _portal_blocked_until - time.time()
    if remaining > 0:
        return True, remaining
    return False, 0.0


def record_portal_401() -> None:
    """Trip the breaker for _PORTAL_BLOCK_COOLDOWN_SECONDS from now. Safe to
    call repeatedly while blocked — each call just re-extends the same
    cooldown window rather than stacking.

    Also auto-connects VPN rotation (lazy import — this module has no VPN
    concerns of its own and must keep working unmodified on machines with
    no VPN configured at all, e.g. Contabo itself). This only became safe
    to do automatically once local_store.py existed: the scraping path
    (pool account claim/release, series checkpointing, search+panel
    saves) now runs entirely local-only while VPN is active (see
    local_store.py's module docstring and api_vpn_connect in app.py),
    instead of each DB call independently burning a 30s PoolTimeout the
    way it did on 2026-10-01 before that existed. Other app routes (the
    homepage, result pages, anything reading live from Postgres) still
    need Contabo reachable and WILL error while VPN is on — that's an
    accepted, known tradeoff from that same session, not something this
    fixes. See clear_portal_block below for the disconnect-and-sync-back
    half of this cycle."""
    with _portal_block_lock:
        global _portal_blocked_until
        _portal_blocked_until = time.time() + _PORTAL_BLOCK_COOLDOWN_SECONDS
    log.warning(
        "IP India portal-wide block detected (401 before login even started) — "
        "pausing all new login attempts for %d minutes.",
        _PORTAL_BLOCK_COOLDOWN_SECONDS // 60,
    )
    if os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1':
        return
    try:
        import requests as _requests
        _requests.post("http://127.0.0.1:5001/api/vpn/connect", timeout=15)
    except Exception:
        log.exception("record_portal_401: auto-connect VPN request failed — "
                       "staying on the %d-minute global wait instead.", _PORTAL_BLOCK_COOLDOWN_SECONDS // 60)


def clear_portal_block() -> None:
    """Called on any successful login — if the portal was flagged blocked
    but a login just went through, the block has clearly lifted already, so
    don't make other callers wait out the rest of a stale cooldown.

    Deliberately does NOT touch VPN here — a successful login THROUGH the
    VPN this same block triggered proves nothing about whether the real IP
    has unblocked (the whole machine routes through the tunnel once
    connected, so there's no way to tell from inside a login attempt which
    IP it actually used). See vpn_rotator.py's _recheck_loop for the
    separate, deliberate disconnect-test-reconnect-if-needed mechanism
    that handles "has the real IP unblocked yet" instead."""
    with _portal_block_lock:
        global _portal_blocked_until
        if _portal_blocked_until > 0.0:
            _portal_blocked_until = 0.0
            log.info("IP India portal-wide block cleared — a login just succeeded.")


class SessionExpiredError(Exception):
    """Raised by execute_search_appno when the portal's own response
    indicates this session's tab_token is no longer valid server-side —
    distinguishable from a transient network error or a wrong CAPTCHA
    answer, both of which stay as ordinary logged failures (return None).
    Callers holding a long-lived (warm) session use this signal to retire
    and re-authenticate that session rather than treating the search itself
    as having failed for an unrelated reason."""
    pass


class PortalBlockedError(Exception):
    """Raised by fetch_captcha when the portal itself returns 401 on the
    CAPTCHA fetch (or the page load immediately before it) — i.e. before any
    login attempt with a specific account has even happened. Distinguishable
    from every other login failure (wrong OTP, unparseable CAPTCHA, a single
    account's own cooldown) so callers can trip the module-wide breaker
    (record_portal_401) instead of burning a pool account's retry budget on
    a request that was never going to succeed regardless of which account
    made it."""
    pass


class PortalDocumentMissing(Exception):
    """The portal answered 404 for a document link; retrying will not help."""


class SearchOutcome(Enum):
    """Unambiguous classification of an application-number search result."""
    HIT = "hit"       # real trademark data found
    MISS = "miss"     # portal responded, no record for this number
    ERROR = "error"   # hard failure: CAPTCHA solve, network, portal rejection


# ── CAPTCHA solver ────────────────────────────────────────────────────────────

_ORDINAL_WORDS = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "sixth": 6, "6th": 6,
}


_ARITHMETIC_RE = re.compile(r"(-?\d+)\s*([+\-*])\s*(-?\d+)")


def solve_captcha_question(question: str) -> int:
    """
    Solve the IP India CAPTCHA. Two distinct question types are seen from
    the portal:

        "Evaluate the Expression : 8 - 2 = ?"     → arithmetic  → 6
        "Enter the Last number in :  2 3 2 8= ?"  → last number → 8
        "Enter the third number in : 3 5 3 6= ?"  → 3rd number  → 3
        "Enter the first number in :  7 1 9 4= ?" → 1st number  → 7

    For the sequence type, the instruction (which position to pick) is
    given in the label before the colon and is NOT always "last".

    Args:
        question: Raw question string from the API.

    Returns:
        The integer answer.

    Raises:
        ValueError: If the question cannot be parsed/solved, or the
            instructed position is out of range.
    """
    label, _, numbers_part = question.partition(":")
    numbers_part = numbers_part.strip()
    label_lower = label.lower()

    # Detect arithmetic structurally (an operator between two numbers)
    # rather than by label wording — the portal uses multiple label phrasings
    # for the same arithmetic question ("Evaluate the Expression",
    # "What is the answer to", etc.) and relying on specific words is fragile.
    arithmetic_match = _ARITHMETIC_RE.search(numbers_part)
    if arithmetic_match:
        left, op, right = arithmetic_match.groups()
        left, right = int(left), int(right)
        if op == "+":
            answer = left + right
        elif op == "-":
            answer = left - right
        else:
            answer = left * right
        log.debug("CAPTCHA solved (arithmetic): %r → %d", question, answer)
        return answer

    digits = [int(tok) for tok in numbers_part.split() if tok.lstrip("-").isdigit()]
    if not digits:
        raise ValueError(f"Cannot parse CAPTCHA question: {question!r}")

    position = None
    for word, idx in _ORDINAL_WORDS.items():
        if word in label_lower:
            position = idx
            break

    if position is not None:
        if position > len(digits):
            raise ValueError(
                f"CAPTCHA asks for position {position} but only {len(digits)} numbers given: {question!r}"
            )
        answer = digits[position - 1]
    else:
        # Default (and "last" instruction) → last number in the sequence.
        answer = digits[-1]

    log.debug("CAPTCHA solved: %r → %d", question, answer)
    return answer


def solve_captcha_with_grok(question: str) -> int:
    """
    Fallback solver using the Grok API (xAI) for CAPTCHA questions that the
    regex parser in solve_captcha_question() cannot handle — e.g. if IP India
    changes the wording/format of the puzzle.

    Raises:
        RuntimeError: if GROK_API_KEY is not configured.
        ValueError: if Grok's response cannot be parsed into an integer.
    """
    if not config.GROK_API_KEY:
        raise RuntimeError("GROK_API_KEY not configured — cannot use Grok fallback solver.")

    resp = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {config.GROK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "grok-4.3",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You solve short math/sequence CAPTCHA questions. "
                        "Reply with ONLY the final integer answer, no words, no punctuation."
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()

    match = re.search(r"-?\d+", text)
    if not match:
        raise ValueError(f"Grok returned no parseable integer: {text!r}")
    answer = int(match.group())
    log.info("CAPTCHA solved via Grok fallback: %r → %d", question, answer)
    return answer


def solve_captcha_auto(question: str) -> int:
    """
    Best-effort automatic solver: try the fast free regex parser first,
    fall back to the Grok API if that fails (e.g. portal changes format).
    """
    try:
        return solve_captcha_question(question)
    except ValueError as exc:
        log.warning("Regex CAPTCHA solver failed (%s), falling back to Grok…", exc)
        return solve_captcha_with_grok(question)


# ── Session factory ───────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    sess = requests.Session()
    # One random UA per session (not per request — a real browser doesn't
    # change UA mid-session, and doing so would be a more detectable
    # inconsistency against cookies that persist across the session, not
    # less). Accept-Language/Accept-Encoding added alongside since a real
    # browser always sends them; their absence is itself a signal. Sec-Ch-Ua
    # deliberately omitted — only valid for Chromium-family UAs and would
    # mismatch if a Firefox UA gets picked.
    sess.headers.update({
        "User-Agent": _random_ua(),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://tmrsearch.ipindia.gov.in",
        "Referer": f"{BASE}/OTP/index",
    })
    # Route through an outbound proxy if one is configured (residential proxy
    # for datacenter-IP deployments — see config.SCRAPE_PROXY_URL). Empty by
    # default = direct connection, unchanged behaviour.
    try:
        import config as _cfg
        if getattr(_cfg, "SCRAPE_PROXY_URL", ""):
            sess.proxies = {"http": _cfg.SCRAPE_PROXY_URL, "https": _cfg.SCRAPE_PROXY_URL}
    except Exception:
        pass
    return sess


# ── Core auth steps ───────────────────────────────────────────────────────────

def fetch_captcha(session: requests.Session) -> tuple[str, int, str]:
    """
    Load the IP India OTP main page (to init session cookies) then fetch
    the CAPTCHA.  Returns (raw_question, auto_answer, numbers_display).

    Raises:
        PortalBlockedError: the portal itself returned 401 on this fetch —
            not an account-specific failure, see that class's docstring.
        requests.RequestException: on network error.
        ValueError: if CAPTCHA cannot be parsed.
    """
    # IMPORTANT: load the main OTP page first so the server sets up the
    # session and generates the CAPTCHA server-side correctly.
    try:
        session.get(f"{BASE}/OTP/index", timeout=15)
        log.debug("OTP main page loaded — session cookies initialised")
    except Exception as exc:
        log.warning("Could not load OTP main page: %s", exc)

    resp = session.get(CAPTCHA_URL, timeout=15)
    if resp.status_code == 401:
        # A brand-new, cookie-less-for-all-practical-purposes request
        # getting 401 here means the portal is rejecting THIS MACHINE, not
        # rejecting a bad login attempt (no credentials have been sent yet
        # at this point in the flow) — see PortalBlockedError's docstring.
        raise PortalBlockedError(
            f"IP India returned 401 Unauthorized on GetCaptcha itself — "
            f"portal-wide block, not an account issue: {resp.text[:200]!r}"
        )
    resp.raise_for_status()
    data = resp.json()
    question = data.get("question", "")
    if not question:
        raise ValueError(f"Unexpected CAPTCHA response: {data!r}")

    # Parse: "Enter the Last number in: 2 3 2 8"
    parts = question.split(":", 1)
    numbers_str = parts[1].strip() if len(parts) > 1 else question
    auto_answer = solve_captcha_auto(question)
    log.debug("CAPTCHA fetched: %r  → auto_answer=%d", question, auto_answer)
    return question, auto_answer, numbers_str


def _parse_json_response(resp: requests.Response, url: str) -> dict:
    """resp.json(), but with a diagnosable error instead of a bare
    JSONDecodeError when the server returns a 2xx with a non-JSON body
    (block/WAF page, empty response, etc. — raise_for_status() doesn't catch
    this since the status code itself is fine)."""
    try:
        return resp.json()
    except ValueError:
        snippet = resp.text[:200].replace("\n", " ").strip()
        raise ValueError(
            f"{url} returned non-JSON (status {resp.status_code}): {snippet!r}"
        )


def send_otp_request(
    session: requests.Session,
    email: str,
    captcha_answer: int,
) -> dict:
    """
    POST to SendOtp.  Returns the JSON response dict.
    {"success": true/false, "message": "...", "waitTime": 120}
    """
    payload = {
        "Email": email,
        "Mobile": None,
        "CaptchaAnswer": captcha_answer,
    }
    resp = session.post(
        SEND_OTP_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_json_response(resp, "SendOtp")


def verify_otp_request(
    session: requests.Session,
    email: str,
    otp_code: str,
) -> dict:
    """
    POST to VerifyOtp.  Returns the JSON response dict.
    {"success": true, "tabToken": "..."}  on success.
    """
    payload = {
        "Email": email,
        "Mobile": None,
        "OtpCode": otp_code,
    }
    resp = session.post(
        VERIFY_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_json_response(resp, "VerifyOtp")


def _is_pdf_response(resp: requests.Response) -> bool:
    """
    Some 'document' links on the portal (reminder notices, exam reports)
    actually render a plain HTML page rather than serving a real PDF, even
    though the site's own UI treats them like a document to view/print.
    Check both the declared Content-Type and the %PDF magic bytes so a
    mislabeled response doesn't get saved with a .pdf extension it can't
    back up.
    """
    content_type = resp.headers.get("Content-Type", "").lower()
    if "pdf" in content_type:
        return True
    if "html" in content_type or "text" in content_type:
        return False
    return resp.content[:5] == b"%PDF-"


def send_keepalive(session: requests.Session, tab_token: str) -> None:
    """Send a keepalive ping so the server-side session doesn't expire."""
    try:
        session.post(
            KEEPALIVE_URL,
            json={"tabToken": tab_token},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception as exc:
        log.warning("Keepalive failed: %s", exc)


# ── High-level authenticated session ─────────────────────────────────────────

class IPIndiaSession:
    """
    Manages a fully-authenticated session with the IP India e-Status portal.

    Usage::

        sess = IPIndiaSession(email="you@gmail.com")
        ok, msg = sess.authenticate(on_status=print)
        if ok:
            results = sess.search_by_name("Tata")
    """

    def __init__(self, email: str, http_guard=None) -> None:
        self.email = email
        self._http = _make_session()
        if http_guard is not None:
            http_guard.install(self._http)
        self.tab_token: Optional[str] = None
        self._ka_thread: Optional[threading.Thread] = None
        self._ka_stop = threading.Event()

    # ── Authentication ────────────────────────────────────────────────────────

    def authenticate(
        self,
        on_status=None,
        max_captcha_retries: int = 5,
        otp_timeout: int = 0,          # 0 = use config value
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Run the full authentication flow automatically.

        Returns:
            (True, "")          on success
            (False, error_msg)  on failure
        """
        def status(msg: str) -> None:
            log.info(msg)
            if on_status:
                on_status(msg)

        timeout = otp_timeout or config.OTP_TIMEOUT_SECONDS

        # Step 1 — fetch CAPTCHA (main page loaded inside fetch_captcha)
        captcha_answer: Optional[int] = None
        for attempt in range(1, max_captcha_retries + 1):
            try:
                status(f"Fetching CAPTCHA (attempt {attempt}/{max_captcha_retries})…")
                _, captcha_answer, numbers = fetch_captcha(self._http)
                status(f"CAPTCHA solved → {captcha_answer} (from sequence: {numbers})")
                break
            except Exception as exc:
                log.warning("CAPTCHA fetch error: %s", exc)
                time.sleep(2)
        else:
            return False, "Could not fetch/solve CAPTCHA after retries."

        # Step 2 — request OTP
        status("Sending OTP request to IP India portal…")
        try:
            send_result = send_otp_request(self._http, self.email, captcha_answer)
        except Exception as exc:
            return False, f"SendOtp request failed: {exc}"

        if not send_result.get("success"):
            msg = send_result.get("message", "Unknown error from SendOtp.")
            return False, f"SendOtp rejected: {msg}"

        status(f"OTP sent to {self.email}. Waiting for it in inbox…")

        # Step 3 — read OTP from email inbox
        if not config.is_otp_configured():
            return False, (
                "Email credentials not configured. "
                "Go to Settings and enter your email + app password."
            )

        otp_code = get_otp(
            email_address=config.OTP_EMAIL_ADDRESS,
            app_password=config.OTP_APP_PASSWORD,
            imap_server=config.OTP_IMAP_SERVER,
            imap_port=config.OTP_IMAP_PORT,
            timeout=timeout,
            poll_interval=config.OTP_POLL_INTERVAL,
            sender_filter=sender_filter or config.OTP_SENDER_FILTER or None,
            subject_filter=subject_filter or config.OTP_SUBJECT_FILTER or None,
        )
        if not otp_code:
            return False, f"OTP not received within {timeout}s. Check your inbox."

        status(f"OTP received. Verifying…")

        # Step 4 — verify OTP
        try:
            verify_result = verify_otp_request(self._http, self.email, otp_code)
        except Exception as exc:
            return False, f"VerifyOtp request failed: {exc}"

        if not verify_result.get("success"):
            msg = verify_result.get("message", "OTP verification failed.")
            return False, msg

        self.tab_token = verify_result.get("tabToken", "")
        status("Authenticated successfully ✓")

        # Step 5 — start keepalive thread
        self._start_keepalive()
        return True, ""

    # ── Keepalive ─────────────────────────────────────────────────────────────

    def _start_keepalive(self) -> None:
        """Start a background thread that pings keepalive every ~55 seconds."""
        self._ka_stop.clear()

        def _loop():
            while not self._ka_stop.wait(_KEEPALIVE_INTERVAL):
                if self.tab_token:
                    send_keepalive(self._http, self.tab_token)

        self._ka_thread = threading.Thread(target=_loop, daemon=True)
        self._ka_thread.start()

    def stop_keepalive(self) -> None:
        self._ka_stop.set()

    def ping_alive(self, timeout: float = 5.0) -> bool:
        """Best-effort liveness probe — POST keepalive and report whether the
        server still accepts this tab_token. Used by warm_pool before handing
        a slot to a search, so a silently-dead session is caught and re-logged
        instead of failing the search with SessionExpiredError.

        Returns True only on an HTTP 2xx whose body does NOT contain the
        'session expired' marker; any exception / non-2xx / expired body =>
        False.
        """
        if not self.tab_token:
            return False
        try:
            resp = self._http.post(
                KEEPALIVE_URL,
                json={"tabToken": self.tab_token},
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
        except Exception:
            return False
        if not resp.ok:
            return False
        try:
            body = resp.text.lower()
        except Exception:
            body = ""
        return _SESSION_EXPIRED_MARKER not in body

    def logout(self, timeout: float = 5.0) -> None:
        """Best-effort clean logout so IP India releases the server-side
        session immediately, instead of leaving it 'unclean' (which blocks
        the next login on this account for ~90s: "previous logout was not
        clean"). Tries the portal's known logout paths; swallows everything.
        """
        if not self.tab_token:
            return
        for path in ("/Otp/Logout", "/Home/Logout", "/Otp/logout", "/Home/logout"):
            try:
                self._http.post(
                    f"{BASE}{path}",
                    json={"tabToken": self.tab_token},
                    headers={"Content-Type": "application/json"},
                    timeout=timeout,
                )
            except Exception:
                pass
        try:
            self.stop_keepalive()
        except Exception:
            pass
        self.tab_token = None


    def get_captcha_for_display(self, max_retries: int = 3) -> dict:
        """
        Load main page + fetch/auto-solve the login CAPTCHA.

        Retries on failure (the portal intermittently 401s GetCaptcha,
        typically when the preceding session-init GET to /OTP/index didn't
        fully take — same flakiness authenticate()'s own CAPTCHA loop
        already tolerates). A human clicking "Start" again absorbed this
        before; callers with nobody to re-click (the pool-mode auto-launch
        path) need it handled here instead.

        Returns a dict with keys:
            label        - the instruction text  (e.g. "Enter the Last number in")
            numbers      - the number sequence    (e.g. "2 3 2 8")
            auto_answer  - the solved answer (regex parser, Grok fallback)
        """
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                question, auto_answer, numbers_str = fetch_captcha(self._http)
                break
            except Exception as exc:
                last_exc = exc
                log.warning("get_captcha_for_display: attempt %d/%d failed: %s", attempt, max_retries, exc)
                if attempt < max_retries:
                    time.sleep(2)
        else:
            # Still retried the normal way (a single 401 here can be a
            # one-off session-init blip, per this method's own docstring) —
            # but if EVERY attempt hit PortalBlockedError specifically, that
            # consistency itself is the signal: this isn't a blip, the
            # portal is genuinely rejecting this machine right now. A plain
            # network error or unparseable CAPTCHA on the last attempt does
            # NOT trip the breaker — only a sustained 401 does.
            if isinstance(last_exc, PortalBlockedError):
                record_portal_401()
            raise last_exc
        parts = question.split(":", 1)
        label = parts[0].strip() if len(parts) > 1 else "Enter CAPTCHA answer"
        return {
            "label":      label,
            "numbers":    numbers_str,
            "auto_answer": auto_answer,
        }

    def authenticate_with_answer(
        self,
        captcha_answer: int,
        on_status=None,
        otp_timeout: int = 0,
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Authenticate using a CAPTCHA answer provided by the user.

        Order of operations (critical for reliability):
          1. Connect to IMAP and snapshot current inbox UIDs
          2. Send OTP request to IP India  (so the OTP email hasn't arrived yet)
          3. Poll inbox for NEW emails only (guaranteed to catch it)

        Returns:
            (True, "")          on success
            (False, error_msg)  on failure
        """
        from email_otp import EmailOTPReader

        def status(msg: str) -> None:
            log.info(msg)
            if on_status:
                on_status(msg)

        timeout = otp_timeout or config.OTP_TIMEOUT_SECONDS

        if not config.is_otp_configured():
            return False, "Email credentials not configured. Go to Settings."

        # ── Step 1: Connect to IMAP and snapshot inbox BEFORE sending OTP ──────
        # This prevents the race condition where the OTP arrives while we are
        # still setting up the IMAP connection and loading existing UIDs.
        status("Connecting to inbox (pre-snapshot)…")
        reader = EmailOTPReader(
            email_address=config.OTP_EMAIL_ADDRESS,
            app_password=config.OTP_APP_PASSWORD,
            imap_server=config.OTP_IMAP_SERVER,
            imap_port=config.OTP_IMAP_PORT,
        )
        if not reader.connect():
            return False, "Could not connect to your email inbox. Check Settings."
        reader.snapshot_current()   # record all UIDs that exist RIGHT NOW

        # ── Step 2: Send OTP request to IP India ─────────────────────────────
        status("Sending OTP request to IP India portal…")
        try:
            send_result = send_otp_request(self._http, self.email, captcha_answer)
        except Exception as exc:
            reader.disconnect()
            return False, f"SendOtp request failed: {exc}"

        if not send_result.get("success"):
            reader.disconnect()
            msg = send_result.get("message", "Unknown error from SendOtp.")
            return False, f"SendOtp rejected: {msg}"

        status(f"OTP sent to {self.email}. Checking inbox…")

        # ── Step 3: Poll inbox — only NEW emails since the snapshot ───────────
        # Use IP India's known sender/subject as filters for speed.
        eff_sender  = sender_filter  or config.OTP_SENDER_FILTER  or "delhi.tmr"
        eff_subject = subject_filter or config.OTP_SUBJECT_FILTER or "OTP"

        otp_code = reader.wait_for_otp(
            timeout=timeout,
            poll_interval=max(1, config.OTP_POLL_INTERVAL - 1),  # faster polling
            sender_filter=eff_sender,
            subject_filter=eff_subject,
        )
        reader.disconnect()

        if not otp_code:
            return False, (
                f"OTP not received within {timeout}s. "
                "Check your inbox — the email comes from delhi.tmr@nic.in."
            )

        status("OTP received ✓  Verifying with IP India…")

        # ── Step 4: Verify OTP ────────────────────────────────────────────────
        try:
            verify_result = verify_otp_request(self._http, self.email, otp_code)
        except Exception as exc:
            return False, f"VerifyOtp request failed: {exc}"

        if not verify_result.get("success"):
            return False, verify_result.get("message", "OTP verification failed.")

        self.tab_token = verify_result.get("tabToken", "")
        status("Authenticated successfully ✓")
        self._start_keepalive()
        return True, ""

    def send_otp_only(self, captcha_answer: int) -> tuple[bool, str]:
        """
        Step-by-step auth: Just send the OTP (no IMAP polling).
        """
        try:
            send_result = send_otp_request(self._http, self.email, captcha_answer)
        except Exception as exc:
            return False, f"SendOtp request failed: {exc}"

        if not send_result.get("success"):
            return False, send_result.get("message", "Unknown error from SendOtp.")
        return True, ""

    def verify_otp_only(self, otp_code: str) -> tuple[bool, str]:
        """
        Step-by-step auth: Verify a provided OTP and start the session.
        """
        try:
            verify_result = verify_otp_request(self._http, self.email, otp_code)
        except Exception as exc:
            return False, f"VerifyOtp request failed: {exc}"

        if not verify_result.get("success"):
            return False, verify_result.get("message", "OTP verification failed.")

        self.tab_token = verify_result.get("tabToken", "")
        self._start_keepalive()
        return True, ""




    # ── Home page ─────────────────────────────────────────────────────────────

    def fetch_home(self) -> Optional[BeautifulSoup]:
        """Fetch the authenticated Home/Index page. Returns BeautifulSoup or None."""
        if not self.tab_token:
            return None
        try:
            resp = self._http.get(
                HOME_URL,
                headers={"X-Tab-Token": self.tab_token},
                timeout=20,
            )
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            log.error("fetch_home failed: %s", exc)
            return None

    # ── Search ────────────────────────────────────────────────────────────────

    def search_by_name(
        self,
        query: str,
        page: int = 1,
    ) -> list[dict]:
        """
        Search trademarks by brand/word mark.

        Tries both common .NET MVC endpoint patterns and falls back to
        parsing the home page HTML for a search form if needed.
        """
        if not self.tab_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        headers = {
            "Content-Type": "application/json",
            "X-Tab-Token": self.tab_token,
        }

        # Common endpoint patterns for ASP.NET Core MVC search
        endpoints_to_try = [
            (SEARCH_URL, {"query": query, "page": page}),
            (f"{BASE}/Home/SearchTrademark", {"wordMark": query, "page": page}),
            (f"{BASE}/Home/GetTrademarks", {"search": query, "pageNo": page}),
        ]

        for url, payload in endpoints_to_try:
            try:
                resp = self._http.post(url, json=payload, headers=headers, timeout=20)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        return _parse_search_response(data, query)
                    except Exception:
                        # Not JSON — try HTML parse
                        soup = BeautifulSoup(resp.text, "html.parser")
                        results = _parse_results_table(soup, query)
                        if results:
                            return results
            except Exception as exc:
                log.debug("Endpoint %s failed: %s", url, exc)

        # Last resort: parse home page for any results
        soup = self.fetch_home()
        if soup:
            return _parse_results_table(soup, query)
        return []

    def _encrypt_rsa(self, public_key_pem: str, plaintext: str) -> Optional[str]:
        """Encrypt string using RSA PKCS#1 v1.5 padding and return base64."""
        try:
            pub_key = load_pem_public_key(public_key_pem.encode("utf-8"))
            ciphertext = pub_key.encrypt(
                plaintext.encode("utf-8"),
                padding.PKCS1v15()
            )
            return base64.b64encode(ciphertext).decode("utf-8")
        except Exception as exc:
            log.error("RSA encryption failed: %s", exc)
            return None

    def prepare_search_appno(self, location: str = "National", max_retries: int = 3) -> Optional[dict]:
        """
        Prepares the search by fetching the AntiforgeryToken, Public Key, and
        the Math CAPTCHA. Retries the whole sequence a few times on failure
        — this is a 4-step chain of portal requests (eregister page ->
        ViewRegisterForm -> public key -> GetCaptcha) and the portal is
        intermittently flaky on any one of them (same class of issue
        get_captcha_for_display already retries for the login CAPTCHA); a
        single network hiccup shouldn't fail the whole search.

        Args:
            location: "National" for National/IRDI Number search (e.g. TM application
                      numbers like 7765565), or "International" for International
                      Registration Number search. Must match one of the radio button
                      values on the /TradeMarkApplication/eregister page.

        Returns a dict with these details, or None if every attempt failed.
        """
        for attempt in range(1, max_retries + 1):
            result = self._prepare_search_appno_once(location)
            if result is not None:
                return result
            if attempt < max_retries:
                log.warning("prepare_search_appno: attempt %d/%d failed, retrying…", attempt, max_retries)
                time.sleep(2)
        return None

    def _prepare_search_appno_once(self, location: str) -> Optional[dict]:
        if not self.tab_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        headers = {
            "Content-Type": "application/json",
            "X-Tab-Token": self.tab_token,
            "Tab-Token": self.tab_token,
        }

        # 1. Load the eregister page to get a fresh AntiforgeryToken for the
        #    Location-select form.
        try:
            resp = self._http.get(
                f"{BASE}/TradeMarkApplication/eregister",
                headers={"X-Tab-Token": self.tab_token},
                timeout=20,
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            anti_forgery_input = soup.find("input", {"name": "AntiforgeryToken"})
            select_token = anti_forgery_input["value"] if anti_forgery_input else ""
        except Exception as exc:
            log.error("Failed to load eregister page: %s", exc)
            return None

        # 2. Submit the Location radio-button form (National vs International).
        #    This is required — the server won't serve the search form otherwise.
        try:
            resp = self._http.post(
                f"{BASE}/TradeMarkApplication/ViewRegisterForm",
                data={"Location": location, "AntiforgeryToken": select_token},
                headers={"X-Tab-Token": self.tab_token},
                timeout=20,
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            anti_forgery_input = soup.find("input", {"name": "AntiforgeryToken"})
            anti_forgery_token = anti_forgery_input["value"] if anti_forgery_input else ""
        except Exception as exc:
            log.error("Failed to submit ViewRegisterForm: %s", exc)
            return None

        # 3. Fetch public key
        try:
            resp = self._http.get(f"{BASE}/Crypto/GetPublicKey", headers=headers, timeout=10)
            public_key_pem = resp.text
        except Exception as exc:
            log.error("Failed to fetch public key: %s", exc)
            return None

        # 4. Fetch second CAPTCHA
        try:
            resp = self._http.get(f"{BASE}/TradeMarkApplication/GetCaptcha", headers=headers, timeout=10)
            c_data = resp.json()
            q_text = c_data.get("question", "")
        except Exception as exc:
            log.error("Failed to fetch search CAPTCHA: %s", exc)
            return None

        # 5. Auto-solve (regex first, Grok fallback) so callers don't need to
        #    prompt the user for an answer.
        auto_answer: Optional[int] = None
        try:
            auto_answer = solve_captcha_auto(q_text)
            log.info("Search CAPTCHA %r solved -> %r", q_text, auto_answer)
        except Exception as exc:
            log.warning("Could not auto-solve search CAPTCHA %r: %s", q_text, exc)

        return {
            "anti_forgery_token": anti_forgery_token,
            "public_key_pem": public_key_pem,
            "captcha_label": q_text,
            "auto_answer": auto_answer,
        }

    def execute_search_appno(
        self,
        app_no: str,
        captcha_answer: str,
        public_key_pem: str,
        anti_forgery_token: str
    ) -> Optional[dict]:
        """
        Executes the search using the manually provided CAPTCHA answer and encryption parameters.
        """
        headers = {
            "Content-Type": "application/json",
            "X-Tab-Token": self.tab_token,
            "Tab-Token": self.tab_token,
        }

        # 1. Encrypt data
        enc_app_no = self._encrypt_rsa(public_key_pem, app_no)
        enc_captcha = self._encrypt_rsa(public_key_pem, captcha_answer)
        if not enc_app_no or not enc_captcha:
            return None

        # 2. Validate CAPTCHA
        try:
            resp = self._http.post(
                f"{BASE}/TradeMarkApplication/ValidateCaptchaAjax",
                json={"encryptedData": enc_captcha},
                headers=headers,
                timeout=10
            )
            resp_json = resp.json()
            if not resp_json.get("success"):
                if _SESSION_EXPIRED_MARKER in str(resp_json.get("message", "")).lower():
                    raise SessionExpiredError(f"ValidateCaptchaAjax: {resp_json.get('message')}")
                log.error("ValidateCaptchaAjax failed: %r (captcha_answer=%r)", resp_json, captcha_answer)
                return None
        except SessionExpiredError:
            raise
        except Exception as exc:
            log.error("ValidateCaptchaAjax error: %s", exc)
            return None

        # 3. Store session data encrypted
        try:
            resp = self._http.post(
                f"{BASE}/TradeMarkApplication/StoreSessionDataEncrypted",
                json={"encryptedData": enc_app_no},
                headers=headers,
                timeout=10
            )
            if not resp.json().get("success"):
                log.error("StoreSessionDataEncrypted failed")
                return None
        except Exception as exc:
            log.error("StoreSessionDataEncrypted error: %s", exc)
            return None

        # 4. Track click — best-effort analytics ping, not required for the
        #    search to succeed. Genuinely best-effort: NEVER abort the
        #    search over this call's outcome, "session expired" included.
        #    Steps 2-3 above (ValidateCaptchaAjax, StoreSessionDataEncrypted)
        #    already proved this session is alive on the exact same tab
        #    token — TrackClick reporting "session expired" immediately
        #    after that is this endpoint's own false positive (observed
        #    live 2026-09-15: killed real, working warm sessions and their
        #    searches for no reason), not a real signal. If the session
        #    really were dead, step 5 (the actual search) will fail on its
        #    own and get handled normally there.
        try:
            resp = self._http.post(f"{BASE}/Home/TrackClick", json={}, headers=headers, timeout=10)
            resp_json = resp.json()
            if not resp_json.get("success"):
                log.warning("TrackClick failed (continuing anyway): status=%s body=%r", resp.status_code, resp_json)
        except Exception as exc:
            log.warning("TrackClick error (continuing anyway): %s", exc)

        # 5. Submit form to ViewRegistration
        form_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Tab-Token": self.tab_token,
            "Tab-Token": self.tab_token,
        }
        form_data = {
            "CaptchModel.CaptchaAnswer": captcha_answer,
            "__Invariant": "CaptchModel.CaptchaAnswer",
        }
        if anti_forgery_token:
            form_data["AntiforgeryToken"] = anti_forgery_token
            
        try:
            resp = self._http.post(
                f"{BASE}/TradeMarkApplication/ViewRegistration",
                data=form_data,
                headers=form_headers,
                timeout=20
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                rows = _parse_results_table(soup, app_no)
                if rows:
                    row = rows[0]
                    row.update(_parse_status_block(soup))
                    token = _extract_view_details_token(soup, app_no)
                    if token:
                        row["_view_token"] = token
                    rvt = _extract_request_verification_token(soup)
                    if rvt:
                        row["_request_verification_token"] = rvt
                    return row
                else:
                    return {"raw_html": resp.text[:500]}
        except Exception as exc:
            log.error("ViewRegistration failed: %s", exc)

        return None

    def get_uploaded_documents(
        self, view_token: str, request_verification_token: Optional[str] = None
    ) -> list[dict]:
        """
        Fetch the "Uploaded Documents" list for a result row (the token
        comes from execute_search_appno()'s result under "_view_token").
        request_verification_token, if given (from that same result's
        "_request_verification_token"), is sent as the RequestVerificationToken
        header — the portal's own JS (myjs/trademarkdeatils.js) always sends
        this on this call; traced live 2026-09-15, ours never had before.

        Returns a list of dicts: {"description": str, "date": str, "document_no": str}
        document_no is the encrypted DOCUMENT_NO query-param needed by
        download_document() to actually fetch the PDF bytes.
        """
        headers = {
            "X-Tab-Token": self.tab_token,
            "Tab-Token": self.tab_token,
        }
        if request_verification_token:
            headers["RequestVerificationToken"] = request_verification_token
        try:
            resp = self._http.post(
                f"{BASE}/RegisteredTM/GetDocumentsDetails",
                data={"token": view_token},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("GetDocumentsDetails failed: %s", exc)
            if os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1':
                raise
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        if os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1':
            from portal_formats import require_list_fragment
            soup = require_list_fragment(resp.text, uploaded=True)
        docs = []
        for table in soup.find_all("table", class_="document-table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                link = cells[3].find("a", href=True)
                if not link:
                    continue
                match = re.search(r"DOCUMENT_NO=([^&\"']+)", link["href"])
                if not match:
                    continue
                docs.append({
                    "description": cells[1].get_text(strip=True),
                    "date": cells[2].get_text(strip=True),
                    "document_no": match.group(1),
                })
        return docs

    def get_pr_details(
        self, view_token: str, request_verification_token: Optional[str] = None
    ) -> Optional[str]:
        """
        Fetch the "PR Details" (Proprietor/Registration history) panel for a
        result row — RegisteredTM/GetPRDetails, confirmed from the portal's
        own myjs/trademarkdeatils.js source (traced live 2026-09-15; not
        previously implemented in this scraper at all). Same token/header
        shape as get_uploaded_documents. Returns the raw HTML fragment the
        portal renders into its #ViewdataModel popup, or None on failure —
        deliberately unparsed here; see extract_document_data-style helpers
        for turning raw fetches like this into structured fields as a
        separate step, not mixed into the fetch itself.
        """
        headers = {"X-Tab-Token": self.tab_token, "Tab-Token": self.tab_token}
        if request_verification_token:
            headers["RequestVerificationToken"] = request_verification_token
        try:
            resp = self._http.post(
                f"{BASE}/RegisteredTM/GetPRDetails",
                data={"token": view_token},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            log.error("GetPRDetails failed: %s", exc)
            return None

    def get_app_details(
        self, view_token: str, request_verification_token: Optional[str] = None
    ) -> Optional[str]:
        """
        Fetch the "e-main page" — the full application-details modal that
        opens when clicking the Trade Mark No. link itself (onclick=
        "ViewAppDetails('<token>')" in the results table). This is
        RegisteredTM/Details, confirmed from the portal's own
        myjs/trademarkdeatils.js source (traced live 2026-09-15) — NOT the
        Get<Name> pattern every other one of these 5 calls follows, so it
        could not have been guessed from naming convention alone. Same
        token/header shape as the others. Returns the raw HTML fragment, or
        None on failure.
        """
        headers = {"X-Tab-Token": self.tab_token, "Tab-Token": self.tab_token}
        if request_verification_token:
            headers["RequestVerificationToken"] = request_verification_token
        try:
            resp = self._http.post(
                f"{BASE}/RegisteredTM/Details",
                data={"token": view_token},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            log.error("RegisteredTM/Details failed: %s", exc)
            return None

    def download_document(self, document_no: str) -> Optional[bytes]:
        """Download the raw PDF bytes for one uploaded document. Returns
        None (not the response body) if the portal didn't actually return
        a real PDF — e.g. a session-expired or generic error page, which
        the portal serves as plain HTML with a 200 status rather than a
        real failure code. Without this check, that HTML page was
        silently saved to disk with a .pdf extension, indistinguishable
        from a real document until opened — see _is_pdf_response's
        docstring for the same issue on the reminder/correspondence
        download paths."""
        headers = {
            "X-Tab-Token": self.tab_token,
            "Tab-Token": self.tab_token,
        }
        try:
            resp = self._http.get(
                f"{BASE}/RegisteredTM/GetDocuments",
                params={"DOCUMENT_NO": document_no},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            if not _is_pdf_response(resp):
                log.error("GetDocuments for %s did not return a real PDF (session expired or portal error page)", document_no)
                return None
            return resp.content
        except Exception as exc:
            log.error("GetDocuments failed: %s", exc)
            return None

    def get_trademark_image(self) -> Optional[bytes]:
        """
        Fetch the trademark's logo/device image directly from the portal's
        own image endpoint (the same <img src="/estatus/trademark/image">
        the ViewRegistration results page itself renders) — no token needed,
        it's keyed off this session's server-side "current application"
        state, which ViewRegistration just set. Must be called right after
        execute_search_appno() / search_appno_classified(), before another
        search on this same session moves that state to a different app_no.

        This is the preferred image source: extract_logo_image() cropping
        the logo out of the TM-A PDF is a fallback for when this direct
        fetch fails (or the PDF itself couldn't be downloaded/parsed) — the
        PDF may be missing/incomplete even when the portal's own rendered
        image is available right here.
        """
        headers = {"X-Tab-Token": self.tab_token, "Tab-Token": self.tab_token}
        try:
            resp = self._http.get(
                f"{BASE}/trademark/image",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").lower()
            if "image" not in content_type:
                return None
            return resp.content
        except Exception as exc:
            log.warning("get_trademark_image failed: %s", exc)
            return None

    def get_reminders(
        self, view_token: str, request_verification_token: Optional[str] = None
    ) -> list[dict]:
        """
        Fetch the "Reminders" list for a result row (same view_token as
        get_uploaded_documents). request_verification_token — see
        get_uploaded_documents's docstring. Returns a list of dicts:
        {"s_no", "corres_no", "corres_date", "subject", "despatch_no",
         "despatch_date", "corres_num"} — corres_num is the identifier
        needed by download_reminder_document() to fetch the actual PDF.
        """
        headers = {"X-Tab-Token": self.tab_token, "Tab-Token": self.tab_token}
        if request_verification_token:
            headers["RequestVerificationToken"] = request_verification_token
        try:
            resp = self._http.post(
                f"{BASE}/RegisteredTM/GetReminders",
                data={"token": view_token},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("GetReminders failed: %s", exc)
            if os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1':
                raise
            return []
        if os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1':
            from portal_formats import require_list_fragment
            require_list_fragment(resp.text)
        return _parse_reminders_table(resp.text)

    def download_reminder_document(self, corres_num: str) -> Optional[tuple[bytes, str]]:
        """
        Download one reminder document. Two-hop: first resolve an encrypted
        one-time URL for corres_num, then fetch that URL (mirrors the
        site's own ViewReminderDocument() JS). Returns (content, ext) where
        ext is "pdf" or "html" — the portal renders these as an HTML page,
        not an actual PDF file, despite the site's own UI treating it like
        a document to view.
        """
        headers = {"X-Tab-Token": self.tab_token, "Tab-Token": self.tab_token}
        try:
            resp = self._http.get(
                f"{BASE}/RegisteredTM/GetEncryptedRemUrl",
                params={"corresNum": corres_num},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            encrypted_url = resp.text.strip().strip('"')
            if not encrypted_url:
                return None
            if not encrypted_url.startswith("http"):
                # The portal returns a path already rooted at the domain
                # (e.g. "estatus/RegisteredTM/ViewReminderDocument?..."),
                # not relative to BASE (which already ends in "/estatus") —
                # joining against BASE directly would double that segment.
                encrypted_url = urllib.parse.urljoin(BASE, "/" + encrypted_url.lstrip("/"))
            doc_resp = self._http.get(encrypted_url, headers=headers, timeout=30)
            doc_resp.raise_for_status()
            ext = "pdf" if _is_pdf_response(doc_resp) else "html"
            return doc_resp.content, ext
        except Exception as exc:
            log.error("Reminder document download failed for %s: %s", corres_num, exc)
            return None

    def get_correspondence(
        self, view_token: str, request_verification_token: Optional[str] = None
    ) -> list[dict]:
        """
        Fetch the "Correspondence & Notices" list for a result row (same
        view_token as get_uploaded_documents). request_verification_token —
        see get_uploaded_documents's docstring. Returns a list of dicts:
        {"s_no", "corres_no", "corres_date", "subject", "despatch_no",
         "despatch_date", "view_url"} — view_url is the relative URL
        (already carrying an encrypted APPLICATION_NUMBER param) needed by
        download_correspondence_document() to fetch the actual PDF.
        """
        headers = {"X-Tab-Token": self.tab_token, "Tab-Token": self.tab_token}
        if request_verification_token:
            headers["RequestVerificationToken"] = request_verification_token
        try:
            resp = self._http.post(
                f"{BASE}/RegisteredTM/GetCorrespondence",
                data={"token": view_token},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("GetCorrespondence failed: %s", exc)
            if os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1':
                raise
            return []
        if os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1':
            from portal_formats import require_list_fragment
            require_list_fragment(resp.text)
        return _parse_correspondence_table(resp.text)

    def download_correspondence_document(self, view_url: str) -> Optional[tuple[bytes, str]]:
        """
        Download one correspondence/notice document, given the relative
        view_url returned by get_correspondence(). Returns (content, ext)
        where ext is "pdf" or "html" — the portal renders these (e.g.
        Examreport) as an HTML page, not an actual PDF file.
        """
        headers = {"X-Tab-Token": self.tab_token, "Tab-Token": self.tab_token}
        url = portal_url(view_url)
        try:
            resp = self._http.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            ext = "pdf" if _is_pdf_response(resp) else "html"
            return resp.content, ext
        except Exception as exc:
            log.error("Correspondence document download failed for %s: %s", view_url, exc)
            # Fleet worker only: let it tell a dead link from a transient failure.
            if (os.environ.get('SCRAPER_EXTERNAL_VPN_CONTROL') == '1'
                    and getattr(getattr(exc, 'response', None), 'status_code', None) == 404):
                raise PortalDocumentMissing(view_url) from None
            return None

    def get_proceeding_page(self, reference: str) -> str:
        """Follow fresh, same-origin opposition links using this tab session."""
        response = self._http.get(portal_url(reference),
                                  headers={'X-Tab-Token': self.tab_token, 'Tab-Token': self.tab_token}, timeout=30)
        response.raise_for_status()
        from portal_formats import require_portal_fragment
        require_portal_fragment(response.text)
        return response.text

    def search_appno_classified(self, app_no: str) -> tuple["SearchOutcome", Optional[dict]]:
        """
        Do one full appno search (fresh CAPTCHA + antiforgery token, as required
        by the portal for every single search) and classify the result
        unambiguously — used by the Auto Discover loop.

        Returns:
            (SearchOutcome.HIT,   result_dict)  — real trademark data found
            (SearchOutcome.MISS,  None)          — portal responded, no record
            (SearchOutcome.ERROR, None)          — CAPTCHA/network/portal failure
        """
        prepare_data = self.prepare_search_appno()
        if not prepare_data or prepare_data.get("auto_answer") is None:
            return SearchOutcome.ERROR, None

        try:
            result = self.execute_search_appno(
                app_no=app_no,
                captcha_answer=str(prepare_data["auto_answer"]),
                public_key_pem=prepare_data["public_key_pem"],
                anti_forgery_token=prepare_data["anti_forgery_token"],
            )
        except SessionExpiredError as exc:
            # This caller (the Auto Discover series loop) has no concept of
            # session pooling/refill — a dead session is just another
            # ERROR outcome to it, same as before SessionExpiredError
            # existed (a "Session expired" TrackClick response used to be
            # swallowed into a logged warning here, not raised).
            log.warning("search_appno_classified: %s", exc)
            return SearchOutcome.ERROR, None
        if result is None:
            return SearchOutcome.ERROR, None
        if "raw_html" in result:
            return SearchOutcome.MISS, None
        return SearchOutcome.HIT, result

    def get_raw_home_html(self) -> str:
        """Return the raw HTML of the authenticated home page (for debugging)."""
        if not self.tab_token:
            return ""
        try:
            resp = self._http.get(HOME_URL, headers={"X-Tab-Token": self.tab_token}, timeout=20)
            return resp.text
        except Exception:
            return ""


TMR_PUBLIC_SEARCH_BASE = "https://tmrsearch.ipindia.gov.in/tmrpublicsearch"


class TmrPublicSearchSession:
    """
    Manages a session with IP India's Wordmark/Vienna/Phonetic PUBLIC search
    portal (tmrpublicsearch) — a completely separate app from the eStatus
    portal IPIndiaSession automates above (different base URL, different
    login endpoints, its own CAPTCHA, its own tabToken stored client-side in
    sessionStorage rather than a server session). It has no Application
    Number search of its own — that's what IPIndiaSession is for — but it's
    the only portal section that supports searching by word mark text AND
    trademark Class together, which eStatus's own search_by_name doesn't do.

    Reverse-engineered by pulling this portal's real JS (myjs/otpbaseddetails.js
    for login, myjs/viewrecord.js for search) since, unlike eStatus, there was
    no prior working implementation to extend.

    Usage::

        sess = TmrPublicSearchSession(pool_account)   # otp_email_pool row dict
        ok, msg = sess.authenticate()
        if ok:
            hits = sess.search_wordmark("TATA", option="contains", class_code=9)
    """

    OPTION_START_WITH = "1"
    OPTION_CONTAINS = "2"
    OPTION_MATCH_WITH = "3"

    def __init__(self, pool_account: dict) -> None:
        # Takes the full otp_email_pool row (not just an email string) so
        # OTP-reading uses THIS account's own IMAP credentials — an earlier
        # version of this class read config.OTP_EMAIL_ADDRESS (a single
        # global default) instead, which only works for whichever one
        # account happens to be configured as the global default and fails
        # silently ("OTP not received") for every other pool account. Same
        # bug class authenticate_pool_account() above already had to solve
        # for IPIndiaSession.
        self.pool_account = pool_account
        self.email = pool_account["email_address"]
        self._http = _make_session()
        self.tab_token: Optional[str] = None

    def authenticate(
        self,
        on_status=None,
        max_captcha_retries: int = 5,
        otp_timeout: int = 0,
    ) -> tuple[bool, str]:
        """Same (ok, error_message) shape as IPIndiaSession.authenticate()
        so callers (pool claim/release, retry-with-backoff) can treat both
        uniformly."""
        def status(msg: str) -> None:
            log.info(msg)
            if on_status:
                on_status(msg)

        timeout = otp_timeout or config.OTP_TIMEOUT_SECONDS

        # Pre-snapshot the inbox BEFORE SendOtp goes out — same fix
        # authenticate_pool_account uses — so an OTP that lands in the gap
        # between the request and the poll starting isn't missed.
        otp_reader: "email_otp.EmailOTPReader | None" = email_otp.EmailOTPReader(
            self.pool_account["email_address"],
            self.pool_account["app_password"],
            self.pool_account["imap_server"],
            self.pool_account["imap_port"],
        )
        if otp_reader.connect():
            otp_reader.snapshot_current()
        else:
            otp_reader = None  # fall back to get_otp()'s own (racy) snapshot

        # Attempt numbers stay in the debug log only (log.warning below) —
        # status() reaches the UI, and a user shouldn't see internal retry
        # counting, just a calm "Connecting…" the whole time it takes.
        status("Connecting…")
        captcha_answer: Optional[int] = None
        for attempt in range(1, max_captcha_retries + 1):
            try:
                resp = self._http.get(f"{TMR_PUBLIC_SEARCH_BASE}/Otp/GetCaptcha", timeout=15)
                resp.raise_for_status()
                question = _parse_json_response(resp, "GetCaptcha").get("question", "")
                captcha_answer = solve_captcha_auto(question)
                break
            except Exception as exc:
                log.warning("[tmrpublicsearch] CAPTCHA fetch error (attempt %d/%d): %s", attempt, max_captcha_retries, exc)
                time.sleep(2)
        else:
            if otp_reader is not None:
                otp_reader.disconnect()
            return False, "Could not fetch/solve login CAPTCHA after retries."

        try:
            resp = self._http.post(
                f"{TMR_PUBLIC_SEARCH_BASE}/Otp/SendOtp",
                json={"Email": self.email, "Mobile": None, "CaptchaAnswer": captcha_answer},
                timeout=20,
            )
            resp.raise_for_status()
            send_result = _parse_json_response(resp, "SendOtp")
        except Exception as exc:
            if otp_reader is not None:
                otp_reader.disconnect()
            return False, f"SendOtp request failed: {exc}"
        if not send_result.get("success"):
            if otp_reader is not None:
                otp_reader.disconnect()
            return False, f"SendOtp rejected: {send_result.get('message', 'Unknown error.')}"

        # No wall-clock min_date — rely on the UID snapshot instead. See
        # authenticate_pool_account's identical fix for why: a wall-clock
        # threshold (originally "now - 15s" computed before CAPTCHA-solve
        # even ran, later shrunk to "now - 2s" after send) is fragile in
        # both directions — too generous lets a previous attempt's stale
        # OTP through, too tight rejects a genuinely fresh one that simply
        # has a Date header a fraction of a second earlier than our local
        # clock. The pre-send UID snapshot has neither failure mode.
        _otp_requested_at = None

        status(f"OTP sent to {self.email}. Waiting for it in inbox…")

        if otp_reader is not None:
            otp_code = otp_reader.wait_for_otp(
                timeout=timeout,
                poll_interval=config.OTP_POLL_INTERVAL,
                sender_filter="nic.in",
                subject_filter="OTP",
                min_date=_otp_requested_at,
            )
            otp_reader.disconnect()
        else:
            otp_code = get_otp(
                email_address=self.pool_account["email_address"],
                app_password=self.pool_account["app_password"],
                imap_server=self.pool_account["imap_server"],
                imap_port=self.pool_account["imap_port"],
                timeout=timeout,
                poll_interval=config.OTP_POLL_INTERVAL,
                sender_filter="nic.in",
                subject_filter="OTP",
                min_date=_otp_requested_at,
            )
        if not otp_code:
            return False, f"OTP not received at {self.email} within {timeout}s. Check the inbox."

        status("OTP received. Verifying…")
        try:
            resp = self._http.post(
                f"{TMR_PUBLIC_SEARCH_BASE}/Otp/VerifyOtp",
                json={"Email": self.email, "Mobile": None, "OtpCode": otp_code},
                timeout=20,
            )
            resp.raise_for_status()
            verify_result = _parse_json_response(resp, "VerifyOtp")
        except Exception as exc:
            return False, f"VerifyOtp request failed: {exc}"
        if not verify_result.get("success"):
            return False, f"OTP verification failed: {verify_result.get('message', 'Invalid OTP')}"

        self.tab_token = verify_result.get("tabToken")
        if not self.tab_token:
            return False, "Login succeeded but no tabToken was returned."

        # Load Home/Index once with the token — mirrors the real browser
        # flow and warms up whatever server-side state the portal expects
        # before a search (it also happens to prime cookies).
        self._headers()
        try:
            self._http.get(f"{TMR_PUBLIC_SEARCH_BASE}/Home/Index", headers=self._headers(), timeout=20)
        except Exception:
            pass

        status("Login complete.")
        return True, ""

    def _headers(self) -> dict:
        return {"X-Tab-Token": self.tab_token or "", "Tab-Token": self.tab_token or ""}

    def _get_search_captcha(self) -> Optional[int]:
        """The search form's CAPTCHA is a DIFFERENT endpoint (Home/GetCaptcha)
        than the login CAPTCHA (Otp/GetCaptcha) — easy to mix up since both
        return the same {"question": "..."} shape."""
        try:
            resp = self._http.get(f"{TMR_PUBLIC_SEARCH_BASE}/Home/GetCaptcha", headers=self._headers(), timeout=15)
            question = resp.json().get("question", "")
            return solve_captcha_auto(question)
        except Exception as exc:
            log.warning("[tmrpublicsearch] search CAPTCHA fetch/solve failed: %s", exc)
            return None

    def search_wordmark(
        self,
        text: str,
        option: str = "contains",
        class_code: int = 0,
        max_retries: int = 3,
        on_batch=None,
        batch_size: int = 25,
    ) -> list[dict]:
        """
        Search by word mark text + trademark Class (both required by this
        portal — it rejects classCode=0/blank client-side, same server-side).

        option: "start_with" | "contains" | "match_with" (or the raw "1"/"2"/"3").

        IP India returns the ENTIRE result set in one HTML response — there is
        no way to fetch it from them incrementally. But a caller can still get
        live progress on a big search (this portal has no page size cap; a
        broad Contains query can return 5000+ rows) by passing on_batch: it's
        called with a list of up to `batch_size` freshly-parsed result dicts
        as soon as they're parsed, well before the rest of the (possibly
        much larger) result set is done parsing — so a job polling loop can
        push rows to the DB/UI as they become available instead of only
        reporting anything once the whole response is fully parsed.

        Returns the full list of result dicts regardless of on_batch — empty
        list on a genuine no-results search, raises on auth/network failure.
        """
        if not self.tab_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        if not text or not text.strip():
            raise ValueError("Word mark text is required.")
        if not class_code or not (1 <= int(class_code) <= 45):
            raise ValueError("A valid trademark Class (1-45) is required.")

        option_map = {
            "start_with": self.OPTION_START_WITH,
            "contains": self.OPTION_CONTAINS,
            "match_with": self.OPTION_MATCH_WITH,
        }
        option_value = option_map.get(option, option)
        if option_value not in (self.OPTION_START_WITH, self.OPTION_CONTAINS, self.OPTION_MATCH_WITH):
            option_value = self.OPTION_CONTAINS

        last_err = ""
        for attempt in range(1, max_retries + 1):
            captcha_answer = self._get_search_captcha()
            if captcha_answer is None:
                last_err = "Could not get a search CAPTCHA."
                time.sleep(2)
                continue

            try:
                resp = self._http.post(
                    f"{TMR_PUBLIC_SEARCH_BASE}/Report/GetWordMarkReport",
                    headers=self._headers(),
                    data={
                        "Searchstring": text.strip().upper(),
                        "Searchtype": "wordmark",
                        "option": option_value,
                        "classCode": int(class_code),
                        "CaptchaAnswer": captcha_answer,
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                last_err = f"Search request failed: {exc}"
                time.sleep(2)
                continue

            if resp.status_code == 200:
                if on_batch is None:
                    return _parse_wordmark_results(resp.text)
                all_results: list[dict] = []
                batch: list[dict] = []
                for row in _iter_wordmark_rows(resp.text):
                    all_results.append(row)
                    batch.append(row)
                    if len(batch) >= batch_size:
                        on_batch(batch)
                        batch = []
                if batch:
                    on_batch(batch)
                return all_results

            # 400 with a short body is almost always "Invalid CAPTCHA" —
            # worth one immediate retry with a fresh captcha rather than
            # failing the whole search over one bad auto-solve.
            last_err = (resp.text or f"HTTP {resp.status_code}")[:200]
            log.warning("[tmrpublicsearch] search attempt %d/%d failed: %s", attempt, max_retries, last_err)
            time.sleep(1.5)

        raise RuntimeError(f"Wordmark search failed after {max_retries} attempt(s): {last_err}")


def _iter_wordmark_rows(html: str):
    """Parse the HTML fragment /Report/GetWordMarkReport returns (a raw
    <table id="tblwrdrpt"> — this endpoint returns a table snippet meant to
    be dropped into a modal, not a full page) and YIELD result dicts one at
    a time. Columns confirmed live: Appl Number, Class, Appl Date, Wordmark,
    Proprietor, Used Since, Valid Upto, Status, Image (inline base64 JPEG),
    Description.

    A generator (not a plain list-returning function) so a caller with a
    5000+ row search can push each row to the DB/job as it's parsed instead
    of holding the entire result set in memory and only reporting progress
    once parsing is fully done — see search_wordmark's on_batch param."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="tblwrdrpt") or soup.find("table")
    if not table:
        return
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 9:
            continue
        app_no_hidden = tr.find("input", id="ApplicationNumber")
        app_no = (app_no_hidden.get("value") if app_no_hidden else "") or cells[1].get_text(strip=True)
        status_badge = cells[8].find("span")
        img_tag = cells[9].find("img") if len(cells) > 9 else None
        yield {
            "app_no": (app_no or "").strip(),
            "class_number": cells[2].get_text(strip=True),
            "application_date": cells[3].get_text(strip=True),
            "trade_mark": cells[4].get_text(strip=True),
            "proprietor_name": cells[5].get_text(strip=True),
            "use_statement": cells[6].get_text(strip=True) or None,
            "valid_upto": cells[7].get_text(strip=True),
            "status": (status_badge.get_text(strip=True) if status_badge else cells[8].get_text(strip=True)),
            "logo_data_uri": (img_tag.get("src") if img_tag and img_tag.get("src", "").startswith("data:") else None),
            "class_description": cells[10].get_text(strip=True) if len(cells) > 10 else None,
        }


def _parse_wordmark_results(html: str) -> list[dict]:
    """Non-streaming convenience wrapper over _iter_wordmark_rows for
    callers that just want the full list (e.g. tests)."""
    return list(_iter_wordmark_rows(html))


# ── Result parsers ────────────────────────────────────────────────────────────

def _parse_search_response(data, query: str) -> list[dict]:
    """Parse a JSON search response into a list of result dicts."""
    results = []
    if isinstance(data, list):
        for item in data:
            results.append(_flatten_result(item, query))
    elif isinstance(data, dict):
        items = (
            data.get("data")
            or data.get("results")
            or data.get("trademarks")
            or data.get("items")
            or []
        )
        if isinstance(items, list):
            for item in items:
                results.append(_flatten_result(item, query))
        elif items:
            results.append(_flatten_result(data, query))
    return results


def _flatten_result(obj, query: str = "") -> dict:
    """Recursively flatten a nested dict into a simple key→value mapping."""
    flat: dict = {"query": query}
    if not isinstance(obj, dict):
        flat["raw"] = str(obj)
        return flat

    def _walk(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                _walk(v, key)
            elif isinstance(v, list):
                flat[key] = ", ".join(str(i) for i in v)
            else:
                flat[key] = v

    _walk(obj)
    return flat


def _parse_results_table(soup: BeautifulSoup, query: str) -> list[dict]:
    """Extract rows from any HTML table on the page."""
    results = []
    for table in soup.find_all("table"):
        headers_els = table.find_all("th")
        if not headers_els:
            continue
        headers = [h.get_text(strip=True) for h in headers_els]
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            record: dict = {"query": query}
            for i, cell in enumerate(cells):
                col = headers[i] if i < len(headers) else f"col_{i}"
                record[col] = cell.get_text(" ", strip=True)
            results.append(record)
    return results


_STATUS_LABELS = ("As on Date", "Status", "Sub Status")


def _parse_status_block(soup: BeautifulSoup) -> dict:
    """
    Extract the "As on Date / Status / Sub Status" header block that sits
    above the results table (a label-only <td>, no <th>, so
    _parse_results_table doesn't pick it up). Returns e.g.:
        {"As on Date": "22-07-2026", "Status": "Formalities Chk Pass",
         "Sub Status": "Not Applicable"}
    """
    info: dict = {}
    for cell in soup.find_all("td"):
        text = cell.get_text(" ", strip=True)
        for label in _STATUS_LABELS:
            if label in info:
                continue
            match = re.match(rf"{re.escape(label)}\s*:\s*(.*)", text)
            if match:
                info[label] = match.group(1).strip()
                break
    return info


def _parse_reminders_table(html: str) -> list[dict]:
    """Parse direct table rows, retaining both onclick and href document links."""
    return parse_notice_table(html)


def _parse_correspondence_table(html: str) -> list[dict]:
    """Parse direct table rows, retaining both onclick and href document links."""
    return parse_notice_table(html)


# ── TM-A application PDF parser ─────────────────────────────────────────────

def _parse_labeled_block(block: str, known_labels: list[str]) -> dict:
    """
    Best-effort parser for a text block where each field is either
    "Label value" on one line, or a bare "Label" line whose value is the
    text line(s) immediately before it (a common layout in the TM-A PDF).
    Multi-line values where the label lands in the middle are not perfectly
    reassembled — good enough for the common single/two-line cases.
    """
    from pdf_scraper.text_fields import labeled_block
    return labeled_block(block, known_labels)


_MOBILE_SHAPE_RE = re.compile(r"^\+?[\d*]{10,11}$")
_EMAIL_SHAPE_RE = re.compile(r"^[\w.*+-]+@[\w.*-]+$")


def _looks_like_mobile(value: Optional[str]) -> Optional[str]:
    """
    Guard against the block parser occasionally attributing a leftover
    address/pincode fragment to a bare "Mobile No." label that's genuinely
    blank in the source form (label-position ambiguity — see
    _parse_labeled_block). Only accept values shaped like a real Indian
    mobile number: 10-11 digits, optionally with masking asterisks, no
    spaces/punctuation — anything else is treated as "field was blank".
    """
    if not value:
        return None
    compact = value.replace(" ", "")
    if not _MOBILE_SHAPE_RE.match(compact):
        return None
    return value


def _looks_like_email(value: Optional[str]) -> Optional[str]:
    """Same guard as _looks_like_mobile, for the Email Address field."""
    if not value or not _EMAIL_SHAPE_RE.match(value):
        return None
    return value


def extract_tma_form_data(pdf_bytes: bytes) -> Optional[dict]:
    """
    Extract structured fields from a TM-A (Trade Marks Application) PDF —
    the document type returned by get_uploaded_documents()/download_document().
    Best-effort: returns whatever fields are found, missing ones are omitted.

    Returns a dict like:
        {
            "temp_ref_no": "14416673", "fee": "4500",
            "application_filed_as": "Individual/Sole Proprietor",
            "applicant": {"name": ..., "address": ..., "country": ..., ...},
            "agent": {"name": ..., "address": ..., ...},
            "mark": {"category": "DEVICE", "trade_mark": "GLOMIX", ...},
            "language": "English",
            "class": {"number": "3", "description": "..."},
            "use_statement": "Proposed to be used",
            "other_information": "...",
            "verification": {"date": "...", "signed_for": "..."},
        }
    Returns None if the PDF doesn't look like a TM-A form (e.g. a scan or an
    unrelated attached document).
    """
    import io

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # The TM-A form content is always on the first couple of pages;
            # later pages are often blank or unrelated attachments.
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    except Exception as exc:
        log.warning("Could not open PDF for text extraction: %s", exc)
        return None

    if "FORM TM- A" not in text and "TRADE MARKS APPLICATION" not in text:
        return None

    def grab(pattern: str, flags: int = 0) -> Optional[str]:
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else None

    data: dict = {}
    data["temp_ref_no"] = grab(r"Temp\. Ref\. No:\s*(\S+)")
    data["fee"] = grab(r"FEE:\s*(\d+)")
    data["application_filed_as"] = grab(r"APPLICATION FILED\n(.+?)\nAS:", re.DOTALL)

    applicant_block = grab(
        r"APPLICANT\s*\n\s*Applicant No\. 1\s*\n(.*?)(?:APPLICANT'S AGENT|MARK DETAILS)",
        re.DOTALL,
    )
    if applicant_block:
        labels = ["Name", "Partners", "Address for Service", "Address",
                   "Country", "Jurisdiction", "Mobile No.", "Email Address",
                   "Nature of Applicant", "Legal Status"]
        a = _parse_labeled_block(applicant_block, labels)
        data["applicant"] = {
            "name": a.get("Name"),
            "partners": a.get("Partners"),
            "address": a.get("Address"),
            "address_for_service": a.get("Address for Service"),
            "country": a.get("Country"),
            "jurisdiction": a.get("Jurisdiction"),
            "mobile": _looks_like_mobile(a.get("Mobile No.")),
            "email": _looks_like_email(a.get("Email Address")),
            "nature": a.get("Nature of Applicant"),
            "legal_status": a.get("Legal Status"),
        }

    agent_block = grab(r"APPLICANT'S AGENT.*?:\s*\n(.*?)MARK DETAILS", re.DOTALL)
    if agent_block:
        labels = ["Name", "Address", "Nature of Agent", "Registration No."]
        ag = _parse_labeled_block(agent_block, labels)
        if ag.get("Name"):
            data["agent"] = {
                "name": ag.get("Name"),
                "address": ag.get("Address"),
                "nature": ag.get("Nature of Agent"),
                "registration_no": ag.get("Registration No."),
            }

    mark_block = grab(r"MARK DETAILS\s*\n(.*?)(?:IF MARK IN A LANGUAGE|Trademark Image:)", re.DOTALL)
    if mark_block:
        labels = ["Category of Mark", "Trade Mark", "Image Description"]
        mk = _parse_labeled_block(mark_block, labels)
        data["mark"] = {
            "category": mk.get("Category of Mark"),
            "trade_mark": mk.get("Trade Mark"),
            "image_description": mk.get("Image Description"),
        }

    data["language"] = grab(r"Language\s+(\S+)")

    class_match = re.search(
        r"CLASS OF GOODS OR SERVICE\s*\nDescription:\s*(.*?)Class:\s*(\d+)\s*(.*?included in Class\s*\d+\.)",
        text, re.DOTALL,
    )
    if class_match:
        desc1 = class_match.group(1).replace("\n", " ").strip()
        class_no = class_match.group(2)
        desc2 = re.sub(r"\s*included in Class\s*\d+\.\s*$", ".", class_match.group(3).replace("\n", " ").strip())
        data["class"] = {
            "number": class_no,
            "description": re.sub(r"\s+", " ", f"{desc1} {desc2}").strip(),
        }

    use_match = re.search(
        r"included in Class\s*\d+\.\s*(.*?)STATEMENT AS TO\s*(.*?)USE OF MARK\s*(.*?)ANY OTHER IMPORTANT INFORMATION",
        text, re.DOTALL,
    )
    if use_match:
        combined = " ".join(g.strip() for g in use_match.groups() if g.strip())
        data["use_statement"] = re.sub(r"\s+", " ", combined).strip()

    other_info = grab(r"ANY OTHER IMPORTANT INFORMATION OR STATEMENT\s*\n(.*?)(?:I hereby verify|VERIFICATION)", re.DOTALL)
    if other_info:
        data["other_information"] = re.sub(r"\s+", " ", other_info).strip()

    verify_date = grab(r"Date\s+([\d\-: ]+(?:AM|PM))")
    verify_for = grab(r"\nfor\s+(.+?)\s*$")
    if verify_date or verify_for:
        data["verification"] = {"date": verify_date, "signed_for": verify_for}

    data["form_type"] = "TM-A"
    return data


def extract_tm1_form_data(pdf_bytes: bytes) -> Optional[dict]:
    """
    Extract structured fields from a TM-1 (older-style Trade Mark
    Application) PDF — a free-form prose document rather than a labeled
    form like TM-A, seen on older/already-registered trademarks.

    Returns a dict like:
        {
            "form_type": "TM-1",
            "receipt_no": "1662111", "receipt_date": "06/02/2017",
            "receipt_amount": "Rs.4000/-", "form_no": "3475667",
            "agent_code": "32253", "proprietor_code": "2042777",
            "mark": {"trade_mark": "BERRY O' JERRY"},
            "class": {"number": "41", "description": "..."},
            "applicant": {"name": "...", "address": "..."},
            "use_statement": "Proposed to be used" | "Used since 01/01/2017",
            "agent": {"address": "..."},  # correspondence address
            "application_date": "06 February 2017",
            "registry_office": "CHENNAI",
        }
    Returns None if the PDF doesn't look like a TM-1 form.
    """
    import io

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
    except Exception as exc:
        log.warning("Could not open PDF for text extraction: %s", exc)
        return None

    if "FORM TM- 1" not in text and "FORM TM-1" not in text:
        return None

    def grab(pattern: str, flags: int = 0) -> Optional[str]:
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else None

    data: dict = {"form_type": "TM-1"}
    data["receipt_no"] = grab(r"Receipt No\.?:\s*(\S+)")
    data["receipt_date"] = grab(r"Receipt No\.?:.*?\nDate:\s*(\S+)", re.DOTALL)
    data["receipt_amount"] = grab(r"Amount:\s*(\S+)")
    data["form_no"] = grab(r"Form No:?\s*(\S+)")
    data["agent_code"] = grab(r"(?:Agent's|Attorney's) Code:\s*(\S+)")
    data["proprietor_code"] = grab(r"Proprietor s? Code No:?\s*(\S+)")

    trade_mark = grab(r"Trademark:\s*([^\n]+)")
    if trade_mark:
        data["mark"] = {"trade_mark": trade_mark}

    class_match = re.search(
        r"in class\s*(\d+)\s*in respect of\s*(.*?)in the name\(s\) of\s*(.*?)who claim",
        text, re.DOTALL,
    )
    if class_match:
        class_no = class_match.group(1).strip()
        class_desc = re.sub(r"\s+", " ", class_match.group(2)).strip()
        data["class"] = {"number": class_no, "description": class_desc}

        proprietor_full = re.sub(r"\s+", " ", class_match.group(3)).strip()
        addr_split = re.split(r"\s*whose address is\s*", proprietor_full, maxsplit=1)
        data["applicant"] = {
            "name": addr_split[0].strip().rstrip("."),
            "address": addr_split[1].strip() if len(addr_split) > 1 else None,
        }

    used_since = grab(r"used since\s*(\d{2}/\d{2}/\d{4})")
    if used_since:
        data["use_statement"] = f"Used since {used_since}"
    elif re.search(r"proposed to be used", text):
        data["use_statement"] = "Proposed to be used"

    correspondence_match = re.search(
        r"following address in India:-\s*\n(.*?)\nDated this", text, re.DOTALL
    )
    if correspondence_match:
        data["agent"] = {"address": re.sub(r"\s+", " ", correspondence_match.group(1)).strip()}

    data["application_date"] = grab(r"Dated this\s*([\d]{1,2}\s+\w+\s+\d{4})")
    data["registry_office"] = grab(r"Trade Marks Registry at\s*(\w+)")

    return data


def extract_document_data(pdf_bytes: bytes) -> Optional[dict]:
    """
    Try each known application-form parser in turn and return the first
    match. Currently recognizes TM-A (newer applications) and TM-1 (older /
    already-registered applications). Returns None if neither matches.
    """
    from pdf_scraper.text_fields import application_fields, merge_fields, pdf_text
    base = extract_tma_form_data(pdf_bytes) or extract_tm1_form_data(pdf_bytes)
    try:
        text, ocr_used = pdf_text(pdf_bytes, with_info=True)
        fields = merge_fields(base, application_fields(text))
        if fields:
            missing = []
            if fields.get('form_type') == 'TM-A':
                if not (fields.get('class') or {}).get('number'):
                    missing.append('class')
                if 'use_statement' not in fields:
                    missing.append('use_statement')
            fields['_extraction'] = {'parser_version': '2', 'ocr_used': ocr_used,
                                     'needs_review': bool(missing) or ocr_used, 'missing_fields': missing}
        return fields
    except Exception:
        if base:
            base = merge_fields(base, {})
            base['_extraction'] = {'parser_version': '2', 'needs_review': True, 'reason': 'Full text/OCR extraction failed'}
        return base


# Portal label -> our field name, for the e-main page's plain label/value
# table (RegisteredTM/Details — see IPIndiaSession.get_app_details). Labels
# are matched with whitespace stripped/collapsed since the portal's own
# markup has inconsistent trailing spaces ("Class ", "Filing Mode ").
_APP_DETAILS_FIELD_MAP = {
    "tm application no.": "app_no",
    "class": "class_number",
    "date of application": "application_date",
    "appropriate office": "office",
    "state": "state",
    "country": "country",
    "filing mode": "filing_mode",
    "tm applied for": "trade_mark",
    "tm category": "category",
    "trade mark type": "tm_type",
    "user detail": "user_detail",
    "certificate detail": "certificate_detail",
    "valid upto/ renewed upto": "valid_upto",
    "proprietor name": "proprietor_name",
    "proprietor address": "proprietor_address",
    "email id": "email",
    "attorney name": "attorney_name",
    "attorney address": "attorney_address",
    "goods & service details": "goods_and_services",
}


def extract_app_details_fields(raw_html: str) -> dict:
    """
    Parse the e-main page's raw HTML (get_app_details's return value) into
    a flat dict of structured fields — State, Office, Country, and the rest
    of _APP_DETAILS_FIELD_MAP above. This is the second-pass "turn raw
    fetched data into structured fields" step this project's fetch/format
    split calls for (see _fetch_all_tm_panels's docstring in app.py) — the
    scraper itself only stores raw_html; this is what a caller runs on top
    of that to get a State/Office/etc. actually worth showing in the UI
    instead of a dumped government HTML table.

    The page is a plain two-cell-per-row label/value table (see a real
    sample: <td bgcolor="lightsteelblue">State</td><td>GUJARAT</td>) with no
    distinguishing class on the label cells — matched here purely by
    normalizing each first-cell's text against _APP_DETAILS_FIELD_MAP,
    which is more robust to the portal's own inconsistent bgcolor/width
    attributes than trying to select on those.

    Returns {} (not None) on anything that doesn't parse — every field is
    optional and callers should treat a missing key as "not available",
    same as the rest of this codebase's extract_* functions.
    """
    if not raw_html:
        return {}
    soup = BeautifulSoup(raw_html, "html.parser")
    fields: dict = {}
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 2:
            continue
        label = re.sub(r"\s+", " ", cells[0].get_text(strip=True)).strip().lower()
        field_name = _APP_DETAILS_FIELD_MAP.get(label)
        if not field_name:
            continue
        # get_text("\n") turns a proprietor/goods cell's <br> line breaks
        # into real newlines instead of losing them by squashing to " ".
        value = cells[1].get_text("\n", strip=True)
        if value:
            fields[field_name] = value
    return fields


def _find_logo_label(page) -> Optional[dict]:
    """Return the "Trademark" word of the "Trademark Image:" label on *page*
    (left-margin position, x0 < 100), or None if not present."""
    for w in page.extract_words():
        if w["text"].strip().rstrip(":").lower() == "trademark" and w["x0"] < 100:
            return w
    return None


def _logo_crop_box(page, label: dict) -> Optional[tuple]:
    """
    Compute a crop box for the logo image cell that starts just below the
    "Trademark Image:" label. Prefers the nearest full-width horizontal
    table line below the label as the bottom bound (tight, avoids bleeding
    into the next form section); falls back to a generous fixed height when
    no such line is found (some older form layouts have no cell border).
    """
    x0 = label["x0"]
    top = label["bottom"] + 2
    max_h = min(page.height - top - 5, 220)
    if max_h < 20:
        return None

    candidates = [
        e["top"] for e in page.edges
        if e["orientation"] == "h" and top < e["top"] <= top + max_h
        and (e["x1"] - e["x0"]) > 400
    ]
    bottom = min(candidates) if candidates else top + min(max_h, 160)

    box_w = min(page.width - x0 - 5, 560)
    if box_w < 20 or (bottom - top) < 20:
        return None
    return (x0, top, x0 + box_w, bottom)


def _autocrop_whitespace(im, tol: int = 245):
    """
    Trim near-white margins so the saved logo image hugs the actual artwork
    instead of the generous outer crop box. Returns None if the box turned
    out to be entirely blank (no logo actually present there).
    """
    from PIL import Image, ImageChops

    rgb = im.convert("RGB")
    bg = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, bg)
    mask = diff.convert("L").point(lambda p: 255 if p > (255 - tol) else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None
    pad = 4
    l, t, r, b = bbox
    l = max(0, l - pad)
    t = max(0, t - pad)
    r = min(im.width, r + pad)
    b = min(im.height, b + pad)
    return im.crop((l, t, r, b))


def extract_logo_image(pdf_bytes: bytes) -> Optional[bytes]:
    """
    Crop the trademark logo/device image out of a TM-A application PDF, so
    it can be shown in the UI without opening the PDF every time.

    Looks for the "Trademark Image:" label on the first two pages and crops
    the region below it. Returns PNG bytes, or None if no reliable anchor
    was found (e.g. a WORD mark with no image, or a poorly-OCR'd scan where
    guessing a crop region risks grabbing the wrong content).
    """
    import io

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[:2]:
                label = _find_logo_label(page)
                if not label:
                    continue
                box = _logo_crop_box(page, label)
                if not box:
                    continue
                pil_im = page.crop(box).to_image(resolution=200).original
                trimmed = _autocrop_whitespace(pil_im)
                if trimmed is None or trimmed.width < 15 or trimmed.height < 15:
                    continue
                out = io.BytesIO()
                trimmed.save(out, format="PNG")
                return out.getvalue()
    except Exception as exc:
        log.warning("Could not extract logo image: %s", exc)
    return None


_VIEW_APP_DETAILS_RE = re.compile(r"ViewAppDetails\('([^']+)'\)")


def _extract_view_details_token(soup: BeautifulSoup, app_no: str) -> Optional[str]:
    """
    Extract the encrypted `token` argument from a result row's
    onclick="ViewAppDetails('<token>')" link. This same token is accepted by
    the portal's Documents()/Reminders()/PRDetails()/Correspondence() calls
    (RegisteredTM/GetDocumentsDetails etc.) — BeautifulSoup already
    HTML-unescapes the attribute value (e.g. &#x2B; -> +).
    """
    link = soup.find("a", string=re.compile(re.escape(app_no)))
    if link is None:
        # Fall back to any ViewAppDetails onclick on the page.
        link = soup.find("a", onclick=_VIEW_APP_DETAILS_RE)
    if link is None:
        return None
    onclick = link.get("onclick", "")
    match = _VIEW_APP_DETAILS_RE.search(onclick)
    return match.group(1) if match else None


def _extract_request_verification_token(soup: BeautifulSoup) -> Optional[str]:
    """
    Extract the __RequestVerificationToken hidden input's value from a
    ViewRegistration result page. Every one of the portal's own
    PRDetails()/ViewAppDetails()/Reminders()/Correspondence()/Documents()
    AJAX calls (see myjs/trademarkdeatils.js — yes, that's really how the
    portal spells it) sends this as a 'RequestVerificationToken' header,
    read client-side via $('input[name="__RequestVerificationToken"]').val()
    — a check our own calls to these same 5 endpoints were missing entirely
    before this was traced from the portal's real source on 2026-09-15.
    """
    tag = soup.find("input", {"name": "__RequestVerificationToken"})
    return tag["value"] if tag and tag.get("value") else None


# ── Pool account login ───────────────────────────────────────────────────────

def authenticate_pool_account(
    pool_account: dict,
    on_release_error=None,
    http_guard=None,
) -> tuple["IPIndiaSession | None", str | None]:
    """
    Fully log in to IP India as *pool_account* (an otp_email_pool row the
    caller already owns — either freshly claimed from the cold pool, or one
    of the reserved warm-pool accounts) — CAPTCHA auto-solve, OTP request,
    auto-read the OTP via IMAP, verify. No human involved at any step.

    Shared by app.py's cold-pool claim path (_pool_claim_and_authenticate)
    and warm_pool.py's warm-slot (re)login — both need the identical login
    sequence, differing only in what "on failure" means for their own
    account-ownership bookkeeping (release back to the cold pool vs. retry
    the same reserved warm account), which is exactly what on_release_error
    exists to let each caller handle its own way.

    Returns (sess, None) on success, or (None, error_message) on failure.
    on_release_error, if given, is called (with no arguments) on any
    failure path so the caller can release/clean up its own claim on this
    account — this function never touches otp_email_pool.claimed_by_job_id
    itself.
    """
    email = pool_account["email_address"]

    # Pre-snapshot the pool inbox BEFORE the OTP request goes out, so an
    # OTP email that lands in the gap between send_otp_only() returning and
    # get_otp() starting its poll isn't mistaken for a pre-existing email
    # and silently ignored (the race that caused false "OTP not received").
    otp_reader: "email_otp.EmailOTPReader | None" = email_otp.EmailOTPReader(
        pool_account["email_address"],
        pool_account["app_password"],
        pool_account["imap_server"],
        pool_account["imap_port"],
    )
    if otp_reader.connect():
        otp_reader.snapshot_current()
    else:
        otp_reader = None  # fall back to get_otp()'s own (racy) snapshot

    def _on_error() -> None:
        if otp_reader is not None:
            otp_reader.disconnect()
        if on_release_error is not None:
            on_release_error()

    blocked, remaining = is_portal_blocked()
    if blocked:
        _on_error()
        return None, (
            f"IP India portal is currently blocking this machine (401 on GetCaptcha) — "
            f"pausing new login attempts for another {int(remaining // 60) + 1} minute(s)."
        )

    sess = IPIndiaSession(email, http_guard=http_guard) if http_guard is not None else IPIndiaSession(email)
    try:
        captcha_info = sess.get_captcha_for_display()
    except Exception as exc:
        log.exception("Pool-mode CAPTCHA fetch failed")
        _on_error()
        return None, f"Could not fetch CAPTCHA from IP India: {exc}"
    clear_portal_block()

    auto_answer = captcha_info.get("auto_answer")
    if auto_answer is None:
        _on_error()
        return None, "Could not auto-solve login CAPTCHA (regex + Grok both failed). Check GROK_API_KEY in Settings."

    ok, err = sess.send_otp_only(auto_answer)
    if not ok:
        _on_error()
        return None, f"Could not send OTP: {err}"

    # Auto-read the OTP via IMAP right here (no human in the loop) instead of
    # rendering the manual OTP-entry screen. The PRIMARY guard against a
    # stale/previous-attempt OTP is otp_reader.snapshot_current(), already
    # called above before send_otp_only() — it records every UID that
    # exists before this attempt even requests an OTP, so a genuinely new
    # email is identified by UID, not wall-clock time, and clock skew
    # between this machine and Gmail's server can't affect it.
    #
    # min_date is a secondary belt-and-suspenders check on TOP of that UID
    # filter, but a wall-clock threshold computed from *our* local time
    # against the email's Date header (set by IP India's mail server) is
    # fundamentally fragile: traced live 2026-09-15, a genuinely-correct,
    # freshly-arrived OTP was rejected as "stale" because its Date header
    # was ~1 second EARLIER than our own post-send timestamp (ordinary
    # send-to-delivery latency variance, not a real staleness problem) —
    # this happened even with the buffer already shrunk down from a much
    # too generous 15s (computed before send_otp_only() even ran, so a
    # PREVIOUS attempt's still-fresh OTP could pass it and get misread as
    # this attempt's code — also a real bug, also fixed here). Backdating
    # further to "cover" sub-second delivery jitter only reopens that
    # original problem, so min_date is dropped in favor of trusting the
    # UID snapshot, which has no such tradeoff.
    _otp_requested_at = None

    if otp_reader is not None:
        otp_code = otp_reader.wait_for_otp(
            timeout=config.OTP_TIMEOUT_SECONDS,
            poll_interval=config.OTP_POLL_INTERVAL,
            sender_filter="nic.in",
            subject_filter="OTP",
            min_date=_otp_requested_at,
        )
        otp_reader.disconnect()
    else:
        otp_code = get_otp(
            email_address=pool_account["email_address"],
            app_password=pool_account["app_password"],
            imap_server=pool_account["imap_server"],
            imap_port=pool_account["imap_port"],
            timeout=config.OTP_TIMEOUT_SECONDS,
            poll_interval=config.OTP_POLL_INTERVAL,
            sender_filter="nic.in",
            subject_filter="OTP",
            min_date=_otp_requested_at,
        )
    if not otp_code:
        _on_error()
        return None, f"OTP not received at {email} within {config.OTP_TIMEOUT_SECONDS}s — pool account released, try again."

    ok, err = sess.verify_otp_only(otp_code)
    if not ok:
        _on_error()
        return None, f"OTP verification failed: {err}"

    return sess, None


# ── One-shot helper ───────────────────────────────────────────────────────────

def authenticate_and_search(
    email: str,
    query: str,
    search_type: str = "name",   # "name" or "appno"
    on_status=None,
) -> tuple[bool, str, list[dict]]:
    """
    Full one-shot: authenticate → search → return results.

    Returns:
        (success: bool, error_message: str, results: list[dict])
    """
    sess = IPIndiaSession(email)
    ok, err = sess.authenticate(on_status=on_status)
    if not ok:
        return False, err, []

    try:
        if search_type == "appno":
            # Requires a manually-solved CAPTCHA — cannot be automated in a
            # one-shot helper. Use prepare_search_appno()/execute_search_appno()
            # directly (see app.py's step 3/4 flow) instead.
            raise NotImplementedError(
                "appno search requires interactive CAPTCHA entry; "
                "use prepare_search_appno()/execute_search_appno() instead."
            )
        else:
            results = sess.search_by_name(query)
    except Exception as exc:
        sess.stop_keepalive()
        return False, str(exc), []

    sess.stop_keepalive()
    return True, "", results
