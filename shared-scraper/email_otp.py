"""
Email OTP reader — connects to an IMAP inbox via SSL and extracts OTP codes.

Uses only Python's built-in imaplib (no extra dependencies needed).

Typical usage
-------------
    from email_otp import get_otp

    otp = get_otp(
        email_address="you@gmail.com",
        app_password="xxxx xxxx xxxx xxxx",
        timeout=60,
        sender_filter="quickcompany",   # optional: only look at emails from this sender
        subject_filter="OTP",           # optional: only look at emails with this subject
    )
    if otp:
        print("Got OTP:", otp)
    else:
        print("OTP not received in time.")
"""

import email
import email.utils
import imaplib
import logging
import re
import ssl
import time
from email.header import decode_header
from typing import Optional

log = logging.getLogger(__name__)

# ── Regex patterns (ordered most-specific → most-generic) ────────────────────
# Each pattern must capture the numeric code in group 1.
_OTP_PATTERNS: list[str] = [
    # Explicit labels first. The optional "\s*(?:is|was|=)?\s*" between the
    # label and the colon/number covers phrasing like IP India's own OTP
    # email ("Your email verification OTP is: 539259") that pattern #1
    # alone didn't match without it — the label here is "OTP", not "OTP is"
    # or "verification code", so neither this nor the old #2 pattern
    # ("your/the code/otp/pin is") fired; traced live 2026-09-15 by pulling
    # a real OTP email and testing this pattern against its actual text.
    r"(?:otp|one[\s\-]?time[\s\-]?(?:password|code)|verification[\s\-]?code|passcode)\s*(?:is|was|=)?\s*[:\s\-]+([0-9]{4,8})",
    r"(?:your|the)\s+(?:code|otp|pin)\s+(?:is|:)\s*([0-9]{4,8})",
    r"use\s+(?:code|otp)\s+([0-9]{4,8})",
]

# Last-resort fallback only, when none of the labeled patterns above matched
# anywhere in the email — a bare digit-run pattern applied to a whole email
# body can just as easily match a date, reference number, or footer digits
# as the real OTP, silently handing back the wrong code (the portal then
# rejects it as "Invalid OTP" with no indication the extraction was the
# actual problem). Kept only for resilience to an unseen template change;
# any match here is logged loudly so a wrong-number bug is diagnosable
# instead of silent.
_OTP_FALLBACK_PATTERNS: list[str] = [
    r"\b([0-9]{6})\b",  # 6-digit (IP India's actual format)
    r"\b([0-9]{4})\b",
    r"\b([0-9]{8})\b",
]


class EmailOTPReader:
    """
    Reads OTP codes from an email inbox via IMAP over SSL.

    Supports use as a context manager::

        with EmailOTPReader(email, password) as reader:
            otp = reader.wait_for_otp(timeout=60)
    """

    def __init__(
        self,
        email_address: str,
        app_password: str,
        imap_server: str = "imap.gmail.com",
        imap_port: int = 993,
    ) -> None:
        self._address = email_address
        self._password = app_password
        self._server = imap_server
        self._port = imap_port
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._seen: Optional[set] = None  # pre-snapshot support

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Open an SSL IMAP connection and authenticate. Returns True on success."""
        try:
            ctx = ssl.create_default_context()
            self._conn = imaplib.IMAP4_SSL(
                self._server, self._port, ssl_context=ctx, timeout=20
            )
            self._conn.login(self._address, self._password)
            log.info("IMAP: authenticated as %s via %s", self._address, self._server)
            return True
        except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
            # NOTE: do not log app_password — log only the error description
            log.error("IMAP: connection failed — %s", exc)
            self._conn = None
            return False

    def disconnect(self) -> None:
        """Close the IMAP session gracefully."""
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            finally:
                self._conn = None

    def snapshot_current(self) -> None:
        """
        Record all email UIDs that already exist in INBOX right now.
        Call this BEFORE sending the OTP request so that wait_for_otp
        only reacts to emails that arrive AFTER the OTP was requested.
        This eliminates the race condition where the OTP arrives while
        the initial snapshot is still being built.
        """
        import datetime
        today_str = datetime.date.today().strftime("%d-%b-%Y")
        self._seen = set()
        try:
            if not self._conn:
                self.connect()
            self._conn.select("INBOX")
            _, data = self._conn.uid("search", None, "SINCE", today_str)
            if data and data[0]:
                self._seen.update(data[0].split())
            log.info("IMAP: pre-snapshot done — %d existing emails today", len(self._seen))
        except Exception as exc:
            log.warning("IMAP: pre-snapshot failed — %s", exc)
            self._seen = set()  # empty set = treat everything as new


    def __enter__(self) -> "EmailOTPReader":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _decode_header_str(raw: str) -> str:
        """Decode an encoded email header value to a plain string."""
        parts = decode_header(raw or "")
        out: list[str] = []
        for chunk, charset in parts:
            if isinstance(chunk, bytes):
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(str(chunk))
        return " ".join(out)

    @staticmethod
    def _plain_text_body(msg: email.message.Message) -> str:
        """Extract readable text from an email.message object — text/plain
        parts as-is, text/html parts with tags stripped. IP India's own OTP
        email (delhi.tmr@nic.in, "Email Verification OTP") is sent as a
        single-part text/html body with NO text/plain part at all: "...OTP
        is: <b>539259</b>". Without stripping tags, the "<b>" sitting right
        between "is:" and the digits breaks every labeled pattern in
        _OTP_PATTERNS (they all require the number to immediately follow the
        label), so extraction silently fell through to the generic
        digit-run fallback on EVERY single OTP read — the actual root cause
        of the "Invalid OTP" failures traced live on 2026-09-15, not a
        timing/race issue as originally suspected."""
        parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type not in ("text/plain", "text/html"):
                    continue
                raw = part.get_payload(decode=True)
                if not raw:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                parts.append(re.sub(r"<[^>]+>", " ", text) if content_type == "text/html" else text)
        else:
            raw = msg.get_payload(decode=True)
            if raw:
                charset = msg.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                parts.append(text)
        return " ".join(parts)

    @staticmethod
    def _extract_otp(text: str) -> Optional[str]:
        """Try each labeled pattern against *text* and return the first
        captured group. Only if none of those match does this fall back to
        the generic digit-run patterns — and it logs when it does, since a
        generic match can grab an unrelated number from the email body."""
        for pattern in _OTP_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1)
        for pattern in _OTP_FALLBACK_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                log.warning(
                    "OTP extracted via generic fallback pattern %r — no labeled "
                    "OTP phrase matched this email; result may be the wrong number.",
                    pattern,
                )
                return m.group(1)
        return None

    def _check_uid(
        self,
        uid: bytes,
        sender_filter: Optional[str],
        subject_filter: Optional[str],
        min_date=None,
    ) -> Optional[str]:
        """Fetch one email by UID, apply filters, and attempt OTP extraction.

        min_date, if given (an aware datetime), rejects any email whose Date
        header is at or before it — so a stale OTP left in the inbox from an
        earlier attempt can't be mistaken for the fresh one just requested.
        """
        try:
            _, msg_data = self._conn.uid("fetch", uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                return None
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            sender = msg.get("From", "")
            subject = self._decode_header_str(msg.get("Subject", ""))

            if sender_filter and sender_filter.lower() not in sender.lower():
                return None
            if subject_filter and subject_filter.lower() not in subject.lower():
                return None
            if min_date is not None:
                try:
                    hdr = msg.get("Date", "")
                    msg_dt = email.utils.parsedate_to_datetime(hdr) if hdr else None
                    if msg_dt is not None and msg_dt <= min_date:
                        log.info("IMAP: skipping stale OTP email (Date %s <= request %s)", msg_dt, min_date)
                        return None
                except Exception:
                    pass  # unparseable Date — don't reject on that alone

            body = self._plain_text_body(msg)
            # Try body first, then subject line
            otp = self._extract_otp(body) or self._extract_otp(subject)
            if otp:
                # Only log last 2 digits of OTP to avoid leaking it in full.
                # Includes which inbox this came from (self._address) — with
                # several pool accounts logging in concurrently, "OTP found"
                # lines were otherwise indistinguishable from each other,
                # making it impossible to tell which account's login a given
                # extraction actually belonged to when diagnosing a failure.
                masked = "*" * (len(otp) - 2) + otp[-2:]
                log.info(
                    "OTP found for %s: %s  (from=%r  subject=%r  uid=%r)",
                    self._address,
                    masked,
                    sender[:60],
                    subject[:80],
                    uid,
                )
            return otp

        except Exception as exc:
            log.warning("IMAP: error reading UID %s — %s", uid, exc)
            return None

    # ── Main API ──────────────────────────────────────────────────────────────

    def wait_for_otp(
        self,
        timeout: int = 60,
        poll_interval: int = 2,
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        min_date=None,
        use_idle: Optional[bool] = None,
    ) -> Optional[str]:
        """
        Wait until a new OTP email arrives or *timeout* seconds elapse.

        If use_idle is True (or None and config.OTP_USE_IDLE is set) and the
        server advertises IDLE, block on an IMAP IDLE push instead of polling
        — the OTP is picked up within a second or two of arriving instead of
        up to poll_interval later. Falls back to polling on any IDLE error.

        min_date, if given, rejects OTP emails dated at/before it (stale-OTP
        guard — see _check_uid).

        Returns the OTP code string, or None on timeout.
        """
        import datetime

        if use_idle is None:
            try:
                import config as _cfg
                use_idle = bool(getattr(_cfg, "OTP_USE_IDLE", False))
            except Exception:
                use_idle = False

        if use_idle:
            idle_start = time.monotonic()
            otp = self._wait_for_otp_idle(
                timeout, sender_filter, subject_filter, min_date, poll_interval
            )
            if otp is not None:
                return otp
            # idle path returned None -> either it ran out its own full
            # `timeout` budget, or it bailed out early (missing idle()
            # support, a caught exception, e.g. the IMAP4.idle() signature
            # mismatch this used to hit on some Python versions). Only the
            # early-bail case needs a real polling fallback — if IDLE
            # already spent the whole timeout, there is no time left to
            # give and capping the fallback at 10s in that case was fine;
            # but capping it at 10s when IDLE bailed in <1s (as it does on
            # AttributeError/exception) was silently truncating every
            # caller's real timeout to 10s — this masked the OTP wait
            # actually being far shorter than configured everywhere IDLE
            # was enabled. Give the fallback whatever time is actually left.
            elapsed = time.monotonic() - idle_start
            remaining = max(0, timeout - elapsed)
            if remaining <= 0:
                return None
            return self._wait_for_otp_poll(
                remaining, poll_interval, sender_filter, subject_filter, min_date
            )

        return self._wait_for_otp_poll(
            timeout, poll_interval, sender_filter, subject_filter, min_date
        )

    def _wait_for_otp_idle(
        self, timeout, sender_filter, subject_filter, min_date, poll_interval
    ) -> Optional[str]:
        """IMAP IDLE fast-path. Returns an OTP, or None to fall back to polling."""
        import datetime

        if not self._conn:
            if not self.connect():
                return None
        try:
            caps = getattr(self._conn, "capabilities", ()) or ()
            if b"IDLE" not in caps and "IDLE" not in caps:
                return None
        except Exception:
            return None

        today_str = datetime.date.today().strftime("%d-%b-%Y")
        seen = set(self._seen) if self._seen is not None else set()
        deadline = time.monotonic() + timeout
        log.info("Waiting for OTP email via IMAP IDLE … (timeout=%ds)", timeout)

        try:
            self._conn.select("INBOX")
            if not seen:
                _, data = self._conn.uid("search", None, "SINCE", today_str)
                if data and data[0]:
                    seen.update(data[0].split())

            while time.monotonic() < deadline:
                # check what's already there first
                _, data = self._conn.uid("search", None, "SINCE", today_str)
                new_uids = [u for u in (data[0].split() if data and data[0] else []) if u not in seen]
                seen.update(new_uids)
                for uid in new_uids:
                    otp = self._check_uid(uid, sender_filter, subject_filter, min_date)
                    if otp:
                        return otp

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # block on a push (Python 3.13+ imaplib.IMAP4.idle context mgr)
                try:
                    with self._conn.idle(duration=min(remaining, 25)) as idle:
                        for _resp in idle:
                            break  # any push (EXISTS/RECENT) — go re-scan
                except AttributeError:
                    # imaplib on this Python has no idle() — fall back
                    return None
                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
                    log.warning("IMAP IDLE: reconnecting after error — %s", exc)
                    try:
                        self.disconnect()
                        self.connect()
                        self._conn.select("INBOX")
                    except Exception:
                        return None
        except Exception as exc:
            log.warning("IMAP IDLE: bailing to polling — %s", exc)
            return None
        return None

    def _wait_for_otp_poll(
        self,
        timeout: int = 60,
        poll_interval: int = 2,
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        min_date=None,
    ) -> Optional[str]:
        """
        Poll INBOX until a new OTP email arrives or *timeout* seconds elapse.
        Uses SINCE today to avoid scanning the whole inbox every poll.
        """
        import datetime

        if not self._conn:
            if not self.connect():
                return None

        deadline = time.monotonic() + timeout

        # Build IMAP SINCE filter (e.g. "22-Jul-2026") so we only look at
        # TODAY'S emails — not your entire inbox of thousands of messages.
        today_str = datetime.date.today().strftime("%d-%b-%Y")

        # Use pre-existing snapshot if caller called snapshot_current() before
        # sending the OTP request; otherwise build one now.
        if self._seen is None:
            seen: set = set()
            try:
                self._conn.select("INBOX")
                _, data = self._conn.uid("search", None, "SINCE", today_str)
                if data and data[0]:
                    seen.update(data[0].split())
                    log.debug("IMAP: %d existing emails today (will skip these)", len(seen))
            except (imaplib.IMAP4.error, OSError) as exc:
                log.warning("IMAP: could not snapshot existing UIDs — %s", exc)
        else:
            seen = self._seen
            log.debug("IMAP: using pre-snapshot with %d existing UIDs", len(seen))

        log.info(
            "Waiting for OTP email … (timeout=%ds, polling every %ds, sender=%r, subject=%r)",
            timeout, poll_interval, sender_filter, subject_filter,
        )

        while time.monotonic() < deadline:
            try:
                self._conn.select("INBOX")
                # Only search TODAY'S emails — much faster than "ALL"
                _, data = self._conn.uid("search", None, "SINCE", today_str)
                if not data or not data[0]:
                    time.sleep(poll_interval)
                    continue

                new_uids = [uid for uid in data[0].split() if uid not in seen]
                if new_uids:
                    log.info("IMAP: %d new email(s) found — checking for OTP…", len(new_uids))
                seen.update(new_uids)

                for uid in new_uids:
                    otp = self._check_uid(uid, sender_filter, subject_filter, min_date)
                    if otp:
                        return otp

            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
                # OSError also covers socket-level failures that aren't
                # imaplib exceptions — e.g. TimeoutError on a stalled
                # SSL recv() when Gmail stops responding mid-poll. Without
                # this, a single flaky network moment kills the whole wait
                # instead of just triggering a reconnect-and-retry.
                log.warning("IMAP: reconnecting after error — %s", exc)
                self.disconnect()
                self.connect()

            time.sleep(poll_interval)

        log.warning("OTP not received within %d seconds.", timeout)
        return None


# ── Convenience functions ─────────────────────────────────────────────────────


def test_connection(
    email_address: str,
    app_password: str,
    imap_server: str = "imap.gmail.com",
    imap_port: int = 993,
) -> dict:
    """
    Attempt an IMAP login and immediately disconnect.

    Returns:
        {"ok": True/False, "message": "human-readable status"}
    """
    reader = EmailOTPReader(email_address, app_password, imap_server, imap_port)
    ok = reader.connect()
    reader.disconnect()
    if ok:
        return {"ok": True, "message": "Connection successful ✓"}
    return {
        "ok": False,
        "message": (
            "Connection failed. Please verify:\n"
            "• Your email address is correct\n"
            "• You are using an App Password (not your main password)\n"
            "• IMAP is enabled in your email account settings\n"
            "• The IMAP server/port is correct"
        ),
    }


def get_otp(
    email_address: str,
    app_password: str,
    imap_server: str = "imap.gmail.com",
    imap_port: int = 993,
    timeout: int = 60,
    poll_interval: int = 3,
    sender_filter: Optional[str] = None,
    subject_filter: Optional[str] = None,
    min_date=None,
) -> Optional[str]:
    """
    One-shot helper: connect → wait for OTP → disconnect → return OTP.

    Prefer this over instantiating EmailOTPReader directly for simple use-cases.
    """
    with EmailOTPReader(email_address, app_password, imap_server, imap_port) as reader:
        return reader.wait_for_otp(
            timeout=timeout,
            poll_interval=poll_interval,
            min_date=min_date,
            sender_filter=sender_filter,
            subject_filter=subject_filter,
        )
