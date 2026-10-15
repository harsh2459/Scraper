"""
Free-tier ProtonVPN WireGuard rotation for harsh-local-01.

Why this exists: IP India occasionally portal-wide-blocks whichever IP this
machine is currently exiting through (see ipindia_scraper.py's
record_portal_401 / PortalBlockedError — that breaker already detects the
401-on-GetCaptcha signal, it just had no way to *act* on it beyond waiting
20 minutes on the same IP). This module gives it somewhere to act: swap to
a different VPN exit server instead of only waiting out the block.

Design, as agreed with the user:
  - Config files live in vpn-configs/*.conf (ProtonVPN's own WireGuard
    exports — one per server, downloaded manually from the Proton account
    dashboard since the free tier has no API/CLI for this).
  - Least-used server gets picked first ("less traffic -> priority") —
    tracked as cumulative connected seconds in vpn-configs/_state.json, not
    actual byte counts (WireGuard's Windows service doesn't expose transfer
    stats without extra tooling, and time-connected is a reasonable proxy
    for load this script itself has caused).
  - A server that trips IP India's block gets marked blocked for 3 hours
    (_BLOCK_COOLDOWN_SECONDS) and skipped by picking until that passes —
    mirrors otp_email_pool's own cooldown-then-eligible-again pattern
    (db.py's _POOL_ACCOUNT_COOLDOWN_SECONDS) rather than inventing a new
    shape for "temporarily unusable, comes back on its own."
  - State is a flat JSON file, not the Postgres DB: this is machine-local
    VPN plumbing, not application data, and must keep working even when
    Postgres itself is unreachable (the exact VPN-vs-Postgres problem this
    project already hit once).

wireguard.exe's tunnel install/uninstall needs admin elevation (it manages
a Windows service) — run this from an elevated shell, or elevate the whole
scraper process, otherwise connect()/disconnect() will raise.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("vpn_rotator")

CONFIG_DIR = Path(__file__).parent / "vpn-configs"
STATE_PATH = CONFIG_DIR / "_state.json"
WIREGUARD_EXE = r"C:\Program Files\WireGuard\wireguard.exe"

_BLOCK_COOLDOWN_SECONDS = 3 * 60 * 60   # 3h, per the user's explicit ask
_USAGE_TICK_SECONDS = 30                # how often the background tracker adds to usage_seconds

_state_lock = threading.Lock()
_active_name: str | None = None         # tunnel name (conf filename w/o .conf) we last connected
_usage_thread_started = False


def _server_name(conf_path: Path) -> str:
    return conf_path.stem


def list_servers() -> list[str]:
    """Every configured VPN server name (conf filename without .conf),
    sorted — new configs the user drops into vpn-configs/ are picked up
    automatically on the next call, no restart needed."""
    return sorted(_server_name(p) for p in CONFIG_DIR.glob("*.conf"))


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("vpn_rotator: _state.json unreadable, starting fresh.")
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)  # atomic on Windows for same-volume renames


def _entry(state: dict, name: str) -> dict:
    return state.setdefault(name, {"usage_seconds": 0.0, "blocked_until": 0.0, "last_connected_at": 0.0})


def pick_next_server(exclude: str | None = None) -> str | None:
    """Least cumulative usage_seconds among servers that aren't currently
    blocked (blocked_until in the past) and aren't *exclude* (normally the
    server that just got blocked, so we never immediately re-pick it even
    if it somehow has low usage). None if every known server is blocked —
    caller should fall back to waiting, not crash."""
    servers = list_servers()
    if not servers:
        log.warning("vpn_rotator: no .conf files in %s", CONFIG_DIR)
        return None

    now = time.time()
    with _state_lock:
        state = _load_state()
        candidates = []
        for name in servers:
            if name == exclude:
                continue
            e = _entry(state, name)
            if e["blocked_until"] > now:
                continue
            candidates.append((e["usage_seconds"], name))
        _save_state(state)  # persist any newly-created entries

    if not candidates:
        log.warning("vpn_rotator: every server is blocked right now.")
        return None

    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def mark_blocked(name: str) -> None:
    """Flag *name* as blocked for _BLOCK_COOLDOWN_SECONDS (3h) — it's
    skipped by pick_next_server until that passes. Safe to call repeatedly;
    each call just re-extends the window, same as ipindia_scraper's own
    record_portal_401 pattern."""
    with _state_lock:
        state = _load_state()
        _entry(state, name)["blocked_until"] = time.time() + _BLOCK_COOLDOWN_SECONDS
        _save_state(state)
    log.warning("vpn_rotator: marked %s blocked for %dh.", name, _BLOCK_COOLDOWN_SECONDS // 3600)


def _add_usage(name: str, seconds: float) -> None:
    with _state_lock:
        state = _load_state()
        e = _entry(state, name)
        e["usage_seconds"] += seconds
        e["last_connected_at"] = time.time()
        _save_state(state)


def current_server() -> str | None:
    """Best-effort: which tunnel service is actually running right now,
    read straight from Windows services rather than trusting our own
    in-memory _active_name (which wouldn't survive a process restart)."""
    try:
        out = subprocess.run(
            ["sc", "query", "state=", "all"], capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return _active_name
    names = set(list_servers())
    for line in out.splitlines():
        m = re.search(r"SERVICE_NAME:\s*WireGuardTunnel\$(.+)", line.strip())
        if m and m.group(1) in names:
            return m.group(1)
    return None


def disconnect(name: str) -> None:
    if not name:
        return
    log.info("vpn_rotator: disconnecting %s", name)
    subprocess.run(
        [WIREGUARD_EXE, "/uninstalltunnelservice", name],
        capture_output=True, text=True, timeout=30,
    )


def connect(name: str, config_dir=None) -> bool:
    """Install+start the tunnel service for *name*. Returns True if the
    service reports running shortly after. wireguard.exe needs admin
    elevation to manage services — a permission error here means the
    calling process itself isn't elevated, not a config problem."""
    conf_path = (Path(config_dir) if config_dir else CONFIG_DIR) / f"{name}.conf"
    if not conf_path.exists():
        log.error("vpn_rotator: %s not found", conf_path)
        return False

    log.info("vpn_rotator: connecting %s", name)
    result = subprocess.run(
        [WIREGUARD_EXE, "/installtunnelservice", str(conf_path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log.error(
            "vpn_rotator: failed to connect %s (rc=%d); verify config and service privileges",
            name, result.returncode,
        )
        return False

    global _active_name
    _active_name = name
    with _state_lock:
        state = _load_state()
        _entry(state, name)["last_connected_at"] = time.time()
        _save_state(state)
    return True


def switch_to(name: str) -> bool:
    """Disconnect whatever's active (if anything) and connect *name*."""
    cur = current_server()
    if cur and cur != name:
        disconnect(cur)
        time.sleep(2)  # let the adapter actually tear down before bringing up the next one
    return connect(name)


def rotate_after_block() -> str | None:
    """Call this when ipindia_scraper detects a portal-wide 401
    (PortalBlockedError) — marks whichever server is currently active as
    blocked, picks the least-used eligible survivor, and switches to it.
    Returns the new server name, or None if every server is blocked (in
    which case the caller falls back to ipindia_scraper's existing 20-
    minute global wait — see record_portal_401 — since there's nothing
    better to switch to)."""
    blocked_name = current_server()
    if blocked_name:
        mark_blocked(blocked_name)

    next_name = pick_next_server(exclude=blocked_name)
    if next_name is None:
        return None

    if switch_to(next_name):
        return next_name
    return None


def ensure_connected() -> str | None:
    """Call once at startup: if no tunnel is active, connect the
    least-used eligible one. Returns the active server name (existing or
    newly connected), or None if nothing could be connected."""
    cur = current_server()
    if cur:
        return cur
    name = pick_next_server()
    if name is None:
        return None
    return name if connect(name) else None


def _usage_tracker_loop() -> None:
    while True:
        time.sleep(_USAGE_TICK_SECONDS)
        name = current_server()
        if name:
            _add_usage(name, _USAGE_TICK_SECONDS)


def start_usage_tracker() -> None:
    """Background daemon thread: every _USAGE_TICK_SECONDS, credits the
    currently-connected server with that much usage_seconds — this is what
    makes 'less-used server gets priority' mean something over time rather
    than every server reading 0 forever. Idempotent; a second call is a
    no-op so app.py can call it unconditionally at startup."""
    global _usage_thread_started
    if _usage_thread_started:
        return
    _usage_thread_started = True
    threading.Thread(target=_usage_tracker_loop, daemon=True).start()
    log.info("vpn_rotator: usage tracker started (tick=%ds).", _USAGE_TICK_SECONDS)


_RECHECK_INTERVAL_SECONDS = 15 * 60  # how often to test whether the real IP has unblocked
_recheck_thread_started = False


def _real_ip_is_unblocked() -> bool:
    """Brief, deliberate disconnect-test-reconnect-if-still-blocked check.
    VPN routes the WHOLE machine, so there's no way to probe IP India on
    the real IP while a tunnel is up except to actually drop it for a
    moment — see the module this is called from (app.py's auto-rotation
    cycle) for why this can't be inferred from a login succeeding through
    the VPN itself (that only proves the VPN exit works, tells us nothing
    about the real IP)."""
    import requests
    cur = current_server()
    if cur is None:
        return True  # nothing connected — "unblocked" is moot, caller has nothing to disconnect anyway
    disconnect(cur)
    time.sleep(2)
    try:
        resp = requests.get("https://tmrsearch.ipindia.gov.in/estatus/Otp/GetCaptcha", timeout=15)
        return resp.status_code != 401
    except Exception:
        return False  # network hiccup during the probe itself — treat as "still blocked", safest default
    finally:
        pass  # caller (the recheck loop) decides whether to reconnect based on the return value


def _recheck_loop() -> None:
    while True:
        time.sleep(_RECHECK_INTERVAL_SECONDS)
        if current_server() is None:
            continue  # nothing to recheck — VPN isn't active right now
        try:
            if _real_ip_is_unblocked():
                log.info("vpn_rotator: real IP works again — staying disconnected, "
                         "local-mode data will sync to Contabo now.")
                import local_store
                local_store.set_local_mode(False)
            else:
                name = pick_next_server()
                if name:
                    connect(name)
                    log.info("vpn_rotator: real IP still blocked — reconnected via %s.", name)
                else:
                    log.warning("vpn_rotator: real IP still blocked and no eligible VPN server left "
                                "— staying disconnected until one frees up (next recheck in %ds).",
                                _RECHECK_INTERVAL_SECONDS)
        except Exception:
            log.exception("vpn_rotator: recheck tick failed — will retry next interval")


def start_recheck_loop() -> None:
    """Background daemon thread: periodically tests whether IP India's
    block on this machine's REAL IP has lifted, so VPN gets dropped
    automatically once it's no longer needed instead of staying connected
    (and keeping local_store.LOCAL_MODE on, meaning other app pages stay
    degraded) indefinitely. Idempotent, like start_usage_tracker."""
    global _recheck_thread_started
    if _recheck_thread_started:
        return
    _recheck_thread_started = True
    threading.Thread(target=_recheck_loop, daemon=True).start()
    log.info("vpn_rotator: real-IP recheck loop started (every %ds).", _RECHECK_INTERVAL_SECONDS)
