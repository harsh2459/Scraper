"""
Retry queue for tma_documents saves that succeeded locally (the PDF file
and its extracted-fields JSON are safely on disk) but couldn't reach
Contabo's Postgres at the time — e.g. this machine's VPN is on for IP
India access and its own database connection is flaky while the VPN is
active. Nothing about the scrape itself is lost in that case: the file is
already saved, only the database ROW is missing until this module
replays it.

Marker files: a small "<name>.pending-sync.json" sits next to the saved
PDF, holding exactly the arguments db.save_tma_document needs. A
background thread (see start_sync_loop) periodically finds these markers
and retries the save; on success, the marker is deleted — the document
then shows up normally everywhere (tma_documents, the fleet dashboard,
etc.), exactly as if the original save had succeeded live.
"""

import json
import logging
import threading
import time
from pathlib import Path

import config
import db
from folder_export import DATA_DIR

log = logging.getLogger(__name__)

_MARKER_SUFFIX = ".pending-sync.json"
_CHECK_INTERVAL_SECONDS = 60


def mark_pending(
    pdf_path: Path,
    app_no: str,
    search_id: int | None,
    extracted: dict,
    logo_path: str | None,
) -> None:
    """Called right after a tma_documents save fails because Contabo's DB
    is unreachable — writes everything needed to retry the save later,
    next to the already-saved PDF."""
    marker_path = pdf_path.with_suffix(pdf_path.suffix + _MARKER_SUFFIX)
    try:
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump({
                "app_no": app_no,
                "search_id": search_id,
                "source_pdf_filename": pdf_path.name,
                "source_pdf_path": str(pdf_path),
                "extracted": extracted,
                "logo_path": logo_path,
            }, f, ensure_ascii=False)
        log.warning(
            "Could not save %s to Contabo's database right now (likely unreachable) — "
            "PDF is saved locally, queued for automatic retry at %s.",
            pdf_path.name, marker_path,
        )
    except Exception:
        log.exception("Could not even write the pending-sync marker for %s — this document's "
                       "database row will need to be re-created manually.", pdf_path)


def _find_pending_markers() -> list[Path]:
    ipindia_dir = DATA_DIR / "ipindia"
    if not ipindia_dir.exists():
        return []
    return list(ipindia_dir.rglob(f"*{_MARKER_SUFFIX}"))


def _replay_one(marker_path: Path) -> bool:
    try:
        with open(marker_path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        log.exception("Could not read pending-sync marker %s — leaving it for manual review.", marker_path)
        return False

    try:
        conn = db.get_connection()
        try:
            db.save_tma_document(
                conn,
                app_no=payload["app_no"],
                search_id=payload.get("search_id"),
                source_pdf_filename=payload["source_pdf_filename"],
                source_pdf_path=payload["source_pdf_path"],
                extracted=payload["extracted"],
                logo_path=payload.get("logo_path"),
            )
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Pending-sync retry for %s still failing (%s) — will try again next check.", marker_path.name, exc)
        return False

    try:
        marker_path.unlink()
    except Exception:
        log.exception("Replayed %s successfully but could not remove its marker file — "
                       "harmless (won't double-insert, save_tma_document just adds another row "
                       "next time), but worth cleaning up by hand.", marker_path)
    log.info("Pending-sync: replayed %s — its tma_documents row is now saved on Contabo.", marker_path.name)
    return True


def _sync_tick() -> None:
    markers = _find_pending_markers()
    if not markers:
        return
    log.info("Pending-sync: found %d document(s) waiting to sync to Contabo — checking now.", len(markers))
    replayed = sum(1 for m in markers if _replay_one(m))
    if replayed:
        log.info("Pending-sync: %d/%d document(s) synced this pass.", replayed, len(markers))


def _sync_loop() -> None:
    while True:
        try:
            _sync_tick()
        except Exception:
            log.exception("Pending-sync tick failed unexpectedly — will retry next interval")
        time.sleep(_CHECK_INTERVAL_SECONDS)


def start_sync_loop() -> None:
    threading.Thread(target=_sync_loop, name="pending-sync", daemon=True).start()
    log.info("Pending-sync: background retry loop started (checks every %ds).", _CHECK_INTERVAL_SECONDS)
