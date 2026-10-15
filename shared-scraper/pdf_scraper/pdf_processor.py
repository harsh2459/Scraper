"""
PDF Scraper (Scraper B) — the actual save + maybe-parse logic.

Kept separate from the Flask route (app.py) so it's testable directly
without going through HTTP. This module never talks to IP India — it only
ever receives bytes that some other process (Scraper A) already downloaded.

Reuses the root scraper's own extraction functions and DB helper directly
(imported, not duplicated) — same server, same venv, same database.
"""

import json
import logging
import mimetypes
import sys
from pathlib import Path

_ROOT_DIR = Path(__file__).parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import pdf_config  # noqa: E402,F401 — registers root config.py under sys.modules["config"] first
import config  # noqa: E402 — the REAL root config.py, thanks to pdf_config's sys.modules trick above
import db  # noqa: E402
from folder_export import DATA_DIR, slugify  # noqa: E402
from google_drive import DriveClient  # noqa: E402
from ipindia_scraper import extract_document_data, extract_logo_image  # noqa: E402

# Only these two kinds ever get content-parsed — every other kind (including
# "reminder"/"correspondence") is saved as-is with extracted always None, per
# the project's explicit scope: only the main application PDF's fields
# matter; reminder/notice dates already come from the portal's HTML list.
_PARSEABLE_KINDS = ("document",)


def _try_drive_upload(app_no: str, filename: str, data: bytes, mimetype: str) -> str | None:
    """Best-effort Drive upload — returns a file id on success, None on any
    failure (Drive not configured, connect failed, folder lookup failed,
    upload failed). Never raises: a None return just means the caller
    should fall back to a local write, exactly like a Drive outage never
    being a hard failure for this app. Opens and discards its own
    short-lived DriveClient per call — uploads here are infrequent enough
    (one per document/logo, not a hot loop) that reusing a connection
    isn't worth the added statefulness."""
    if not config.is_drive_configured():
        return None
    try:
        drive = DriveClient()
        if not drive.connect():
            return None
        folder_id = drive.get_or_create_app_folder(app_no)
        if not folder_id:
            return None
        return drive.upload_bytes(data, filename, folder_id, mimetype)
    except Exception:
        logging.exception("Drive upload failed for %s/%s", app_no, filename)
        return None


def process_pdf(
    file_bytes: bytes,
    app_no: str,
    description: str,
    kind: str = "document",
    search_id: int | None = None,
    ext: str = "pdf",
) -> dict:
    """Store *file_bytes* — Google Drive first, local disk only as a
    fallback if Drive is unreachable/misconfigured — and, only for
    kind="document", attempt TM-A/TM-1 field extraction.

    Storage: when config.is_drive_configured() is True, this uploads
    straight to Drive from the in-memory bytes and, on success, NEVER
    writes a local file at all — the server's disk usage for new
    documents should stay flat from here on (see the 2026-09-22 decision:
    a personal 5TB Drive account replaces local storage, not just backs
    it up). Only when the Drive upload fails (not configured, network
    issue, quota, any exception) does this fall back to writing the local
    file exactly as it always did before this feature existed — nothing
    is ever lost, Drive being down just means the file stays local until
    the separate cleanup script (scratch/backfill_drive_migration.py)
    picks it up later.

    ext: the real file extension without a leading dot (e.g. "pdf" or
    "html" — reminder/correspondence downloads can come back as plain HTML
    pages rather than real PDFs, see ipindia_scraper.download_reminder_
    document / download_correspondence_document).

    Returns {"saved_path": str, "extracted": dict | None}. saved_path is
    always a real filename-bearing string (the ONLY confirmed caller,
    app.py's PDF-scraper client, only ever reads Path(saved_path).name —
    never re-opens the file — so this stays safe to return even when
    nothing was actually written to that path locally).

    Raises on a genuine failure to write the FALLBACK local file (caller —
    the Flask route — turns that into a 500; Scraper A's caller falls back
    to saving the file itself in that case, so nothing is silently lost).
    """
    folder = DATA_DIR / "ipindia" / slugify(app_no)
    if kind == "reminder":
        folder = folder / "reminders"
    elif kind == "correspondence":
        folder = folder / "correspondence"

    name = slugify(description) or "document"
    ext = (ext or "pdf").lstrip(".")
    base_filename = f"{name}.{ext}"

    if kind in _PARSEABLE_KINDS:
        # Check the DATABASE first, not just local disk — with Drive-first
        # uploads, a document already saved (on Drive, no local file at
        # all) looked "new" on every later fetch of the same app_no, since
        # the old local-disk-only collision check below never saw it. That
        # caused the same document to be re-downloaded and re-inserted as
        # a fresh tma_documents row under a new -N suffix on every re-fetch
        # (staleness refresh, retries) instead of being recognized as
        # already saved. The database is the one shared source of truth a
        # fleet of worker machines all see, so check it first.
        existing_conn = db.get_connection()
        try:
            existing = db.find_tma_document_by_base_name(existing_conn, app_no, base_filename)
        finally:
            existing_conn.close()
        if existing is not None:
            return {"saved_path": existing["source_pdf_path"] or existing["source_pdf_filename"], "extracted": None}

    # Collision-safe filename, checked against whatever's ALREADY on local
    # disk (unaffected by the Drive-first change — Drive filenames are
    # kept unique by being namespaced under one folder per app_no, no
    # collision-suffix logic needed there).
    path = folder / f"{name}.{ext}"
    i = 1
    while path.exists():
        i += 1
        path = folder / f"{name}-{i}.{ext}"

    drive_filename = f"{kind}__{path.name}"
    mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    drive_file_id = _try_drive_upload(app_no, drive_filename, file_bytes, mimetype)

    wrote_locally = False
    if drive_file_id is None:
        # Drive not configured, or the upload failed — fall back to the
        # exact local-write behavior this function always had.
        folder.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(file_bytes)
        wrote_locally = True

    extracted: dict | None = None
    if kind in _PARSEABLE_KINDS:
        try:
            extracted = extract_document_data(file_bytes)
        except Exception:
            logging.exception("Could not extract fields from document %s", path)
            extracted = None

        if extracted:
            # JSON sidecar stays local always — small structured metadata,
            # not part of the storage problem this migration addresses,
            # and several other parts of this app (e.g. re-parsing without
            # re-fetching) may expect it to exist locally. Only written
            # when the main document also has a local presence would be
            # inconsistent with today's behavior otherwise, so this is
            # unconditional exactly like before.
            try:
                folder.mkdir(parents=True, exist_ok=True)
                json_path = path.with_suffix(".json")
                with open(json_path, "w", encoding="utf-8") as jf:
                    json.dump(extracted, jf, indent=2, ensure_ascii=False)
            except Exception:
                logging.exception("Could not write JSON sidecar for %s", path)

            logo_path_str: str | None = None
            logo_drive_file_id: str | None = None
            try:
                logo_bytes = extract_logo_image(file_bytes)
                if logo_bytes:
                    logo_filename = f"{path.stem}-logo.png"
                    logo_drive_file_id = _try_drive_upload(app_no, f"logo__{logo_filename}", logo_bytes, "image/png")
                    if logo_drive_file_id is None:
                        # Same Drive-first-then-local-fallback rule as the
                        # main document above.
                        folder.mkdir(parents=True, exist_ok=True)
                        logo_path = path.with_name(logo_filename)
                        with open(logo_path, "wb") as lf:
                            lf.write(logo_bytes)
                        logo_path_str = str(logo_path)
            except Exception:
                logging.exception("Could not extract logo image from document %s", path)

            conn = db.get_connection()
            try:
                db.save_tma_document(
                    conn,
                    app_no=app_no,
                    search_id=search_id,
                    source_pdf_filename=path.name,
                    source_pdf_path=str(path) if wrote_locally else None,
                    extracted=extracted,
                    logo_path=logo_path_str,
                    drive_file_id=drive_file_id,
                    logo_drive_file_id=logo_drive_file_id,
                )
            finally:
                conn.close()

    return {"saved_path": str(path), "extracted": extracted}
