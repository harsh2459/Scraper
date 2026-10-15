"""
Per-process, in-memory session tracker for the local status page
(GET /status, GET /api/worker-status) — mirrors warm_pool.py's own
lock + module-level-dict style.

A "session" here means one claimed-account holding for a whole batch (see
app.py's _run_batch_job, which claims ONE warm/cold session and holds it
across every app_no in the batch) — not one entry per individual trademark.
Counts reset whenever this process restarts; there is no persistence, by
design (this is a live "what's this machine doing right now" view, not a
historical record — see tm_manager's fleet dashboard / Postgres for that).
"""

import threading
import time

_lock = threading.Lock()

# email -> {"app_no": str | None, "started_at": float, "job_id": str}
# Present in this dict == that account currently holds a claimed session.
_inflight: dict[str, dict] = {}

# email -> {"success": int, "failure": int} — cumulative since process start.
_counts: dict[str, dict] = {}


def start_session(email: str, job_id: str) -> None:
    with _lock:
        _inflight[email] = {"app_no": None, "started_at": time.time(), "job_id": job_id}
        _counts.setdefault(email, {"success": 0, "failure": 0})


def set_current_appno(email: str, app_no: str | None) -> None:
    with _lock:
        if email in _inflight:
            _inflight[email]["app_no"] = app_no


def end_session(email: str, success: bool) -> None:
    with _lock:
        _inflight.pop(email, None)
        c = _counts.setdefault(email, {"success": 0, "failure": 0})
        c["success" if success else "failure"] += 1


def snapshot() -> dict:
    """Read-only view for the status route — inflight sessions (with how
    long each has been running) plus cumulative counts, per account."""
    with _lock:
        now = time.time()
        inflight = {
            email: {**info, "elapsed_secs": round(now - info["started_at"], 1)}
            for email, info in _inflight.items()
        }
        counts = {email: dict(c) for email, c in _counts.items()}
    return {"inflight": inflight, "counts": counts}
