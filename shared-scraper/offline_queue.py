"""
Full-hit offline queue for Auto Discover / series-scan background jobs —
extends pending_sync.py's "save what you can locally, replay to Contabo
later" pattern to cover the WHOLE hit (the ipindia_results search row
itself, plus every panel save that depends on its search_id), not just the
one tma_documents row pending_sync.py already handles.

Why this exists: pending_sync.py only kicks in AFTER db.save_ipindia_search
already succeeded (it needs a real search_id to attach a document to). If
Postgres is unreachable from the very start of a hit — the realistic
overnight-with-VPN case — save_ipindia_search itself fails, there is no
search_id yet, and pending_sync.py has nothing to attach a document to.
This module queues from that earlier point instead.

Scope, deliberately narrow (see the conversation this was built from):
only _record_discover_hit (shared by both Auto Discover and series-scan
jobs — see app.py) and the panel-fetch/save helpers it calls go through
this queue. Manual/live TM Search, the results page, fleet batch jobs,
and anything else that reads its own write back in the same request are
UNCHANGED — they still require Postgres to be reachable, exactly as
before this module existed, because there is no fake-then-remap answer
for "show the user their own search result right now."

Replay order matters: tma_documents/ipindia_reminders/ipindia_correspondence/
ipindia_page_fetches.search_id all have a real foreign key to
ipindia_results(id) (ON DELETE SET NULL, but still enforced on INSERT) —
see db.py's BILLING_SCHEMA sibling, the main schema. So a queued bundle is
always replayed as: save_ipindia_search first (gets a REAL id), then every
panel save using that real id — never the other order.

Bundle file shape: one JSON file per hit in data/_offline_queue/, holding
everything save_ipindia_search and the panel-save functions need. Documents
(PDFs) are NOT included here — _save_uploaded_documents already writes PDF
bytes straight to disk regardless of DB reachability and separately queues
its own tma_documents row via pending_sync.mark_pending (unchanged); this
module's "documents" list below only carries the already-extracted field
data pending_sync would otherwise need a search_id for, so that document
row rides along with the rest of this same hit's replay instead of needing
pending_sync's own retry loop to separately guess a search_id later.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

import db
from folder_export import DATA_DIR

log = logging.getLogger(__name__)

QUEUE_DIR = DATA_DIR / "_offline_queue"
_CHECK_INTERVAL_SECONDS = 60


def new_bundle(app_no: str, search_type: str, email_used: str, results: list[dict], error: str = "") -> dict:
    """Start a bundle for one hit — call this instead of db.save_ipindia_search
    when you need to be able to fall back to queuing. Pass the returned dict
    into add_document/add_reminder/add_correspondence/add_page_fetch as the
    panel fetches complete, then queue_bundle(bundle) only if the live DB
    path actually failed."""
    return {
        "bundle_id": str(uuid.uuid4()),
        "queued_at": time.time(),
        "search": {
            "app_no": app_no, "search_type": search_type,
            "email_used": email_used, "results": results, "error": error,
        },
        "documents": [],
        "reminders": [],
        "correspondence": [],
        "page_fetches": [],
    }


def add_document(bundle: dict, *, source_pdf_filename: str, source_pdf_path: str | None,
                  extracted: dict, logo_path: str | None,
                  drive_file_id: str | None = None, logo_drive_file_id: str | None = None) -> None:
    bundle["documents"].append({
        "source_pdf_filename": source_pdf_filename, "source_pdf_path": source_pdf_path,
        "extracted": extracted, "logo_path": logo_path,
        "drive_file_id": drive_file_id, "logo_drive_file_id": logo_drive_file_id,
    })


def add_reminder(bundle: dict, *, row: dict, document_path: str | None) -> None:
    bundle["reminders"].append({"row": row, "document_path": document_path})


def add_correspondence(bundle: dict, *, row: dict, document_path: str | None) -> None:
    bundle["correspondence"].append({"row": row, "document_path": document_path})


def add_page_fetch(bundle: dict, *, kind: str, raw_html: str | None) -> None:
    bundle["page_fetches"].append({"kind": kind, "raw_html": raw_html})


def queue_bundle(bundle: dict) -> None:
    """Persist *bundle* to disk for later replay. Call this when
    db.save_ipindia_search (or any save made with the real search_id)
    raised — everything gathered via add_* so far is preserved, nothing
    re-fetched from IP India on replay, only re-saved to Postgres."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    path = QUEUE_DIR / f"{bundle['bundle_id']}.json"
    try:
        path.write_text(json.dumps(bundle, ensure_ascii=False))
        log.warning(
            "offline_queue: Contabo unreachable — queued hit for app_no=%s locally "
            "(%d document(s), %d reminder(s), %d correspondence row(s)) for later sync.",
            bundle["search"]["app_no"], len(bundle["documents"]),
            len(bundle["reminders"]), len(bundle["correspondence"]),
        )
    except Exception:
        log.exception("offline_queue: could not write bundle for app_no=%s — this hit's "
                       "database rows will need to be re-created manually.", bundle["search"]["app_no"])


def _find_bundles() -> list[Path]:
    if not QUEUE_DIR.exists():
        return []
    return sorted(QUEUE_DIR.glob("*.json"))


def _replay_bundle(bundle: dict) -> int:
    """Shared replay core for both this module's own disk-marker bundles
    AND local_store's SQLite-held bundles (same shape — see
    local_store.save_local_hit) — returns the real search_id. Raises on
    any failure; callers decide what "still pending" means for their own
    storage (unlink a file vs. delete a SQLite row)."""
    conn = db.get_connection()
    try:
        s = bundle["search"]
        search_id = db.save_ipindia_search(
            conn, s["app_no"], s["search_type"], s["email_used"], s["results"], error=s.get("error", ""),
        )
        for d in bundle["documents"]:
            db.save_tma_document(
                conn, s["app_no"], search_id,
                source_pdf_filename=d["source_pdf_filename"], source_pdf_path=d["source_pdf_path"],
                extracted=d["extracted"], logo_path=d.get("logo_path"),
                drive_file_id=d.get("drive_file_id"), logo_drive_file_id=d.get("logo_drive_file_id"),
            )
        for r in bundle["reminders"]:
            db.save_ipindia_reminder(conn, s["app_no"], search_id, r["row"], r.get("document_path"))
        for c in bundle["correspondence"]:
            db.save_ipindia_correspondence(conn, s["app_no"], search_id, c["row"], c.get("document_path"))
        for p in bundle["page_fetches"]:
            db.save_ipindia_page_fetch(conn, s["app_no"], search_id, p["kind"], p.get("raw_html"))
        return search_id
    finally:
        conn.close()


def _replay_one(path: Path) -> bool:
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("offline_queue: could not read bundle %s — leaving it for manual review.", path)
        return False

    try:
        search_id = _replay_bundle(bundle)
    except Exception as exc:
        log.warning("offline_queue: replay for %s still failing (%s) — will try again next check.", path.name, exc)
        return False

    try:
        path.unlink()
    except Exception:
        log.exception("offline_queue: replayed %s successfully but could not remove it — "
                       "harmless (won't double-insert on its own, nothing re-reads this file "
                       "once replay succeeds), but worth cleaning up by hand.", path.name)
    log.info("offline_queue: replayed queued hit for app_no=%s — now saved on Contabo (search_id=%s).",
              bundle["search"]["app_no"], search_id)
    return True


def _sync_local_store_hits() -> None:
    """Replay everything local_store.py collected while VPN/local mode was
    on — same replay core as this module's own disk markers, just a
    different source. Runs every tick regardless of whether local mode is
    CURRENTLY on, so hits left over from an earlier VPN session still get
    synced even after VPN is switched off (that's the whole point: local
    mode only controls where NEW hits go, this loop doesn't care)."""
    import local_store
    bundles = local_store.get_unsynced_hits()
    if not bundles:
        return
    log.info("offline_queue: found %d local-store hit(s) waiting to sync to Contabo — checking now.", len(bundles))
    replayed = 0
    for bundle in bundles:
        try:
            search_id = _replay_bundle(bundle)
        except Exception as exc:
            log.warning("offline_queue: local-store replay for app_no=%s still failing (%s) — "
                        "will try again next check.", bundle["search"]["app_no"], exc)
            continue
        local_store.mark_hit_synced(bundle["bundle_id"])
        log.info("offline_queue: synced local-store hit for app_no=%s (search_id=%s).",
                  bundle["search"]["app_no"], search_id)
        replayed += 1
    if replayed:
        log.info("offline_queue: %d/%d local-store hit(s) synced this pass.", replayed, len(bundles))


def _sync_tick() -> None:
    bundles = _find_bundles()
    if bundles:
        log.info("offline_queue: found %d queued hit(s) waiting to sync to Contabo — checking now.", len(bundles))
        replayed = sum(1 for b in bundles if _replay_one(b))
        if replayed:
            log.info("offline_queue: %d/%d queued hit(s) synced this pass.", replayed, len(bundles))
    _sync_local_store_hits()


def _sync_loop() -> None:
    while True:
        try:
            _sync_tick()
        except Exception:
            log.exception("offline_queue: sync tick failed unexpectedly — will retry next interval")
        time.sleep(_CHECK_INTERVAL_SECONDS)


def start_sync_loop() -> None:
    threading.Thread(target=_sync_loop, name="offline-queue-sync", daemon=True).start()
    log.info("offline_queue: background replay loop started (checks every %ds).", _CHECK_INTERVAL_SECONDS)
