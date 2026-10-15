"""
Google Drive client for this app's document storage migration — a personal
Google account's own Drive (5TB free tier used here), accessed via OAuth2
with a long-lived refresh token (NOT a service account: a service account
has no access to a personal account's own storage quota). See
scripts/google_drive_auth_setup.py for the one-time consent flow that
produces GOOGLE_DRIVE_REFRESH_TOKEN.

Scope requested during that consent flow: drive.file only — this app can
only see/manage files it itself created through this client, never browse
or touch anything else already in the account's Drive.

Typical usage
-------------
    from google_drive import DriveClient

    with DriveClient() as drive:
        folder_id = drive.get_or_create_app_folder("1234567", cached_folder_id=None)
        file_id = drive.upload_bytes(pdf_bytes, "document__application.pdf", folder_id, "application/pdf")
        data = drive.download_bytes(file_id)

Folder layout on the Drive side: ONE folder per app_no directly under
GOOGLE_DRIVE_ROOT_FOLDER_ID (created lazily, its id cached in
my_trademarks.drive_folder_id so a lookup-or-create call is only ever
needed once per app_no) — never nested "reminders"/"correspondence"
subfolders. The document's kind is prefixed into the filename instead
(e.g. "reminders__notice-of-abandonment.html"). This keeps folder-creation
calls to one per app_no instead of up to three, which matters far more at
50,000+ trademarks than Drive's own per-user rate limits do.

No delete-from-Drive method exists here by design — once a file is
uploaded, Drive is the new source of truth for it; this client only ever
needs to create and read.
"""

import io
import logging
from typing import Optional

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

log = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_FOLDER_MIMETYPE = "application/vnd.google-apps.folder"


class DriveClient:
    """
    Thin wrapper around the Drive v3 API, mirroring this codebase's
    EmailOTPReader conventions: credentials sourced from config by default,
    a connect() step, context-manager support, exceptions caught and
    logged rather than propagated, a fresh instance per use rather than a
    global mutable singleton holding a live connection.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        root_folder_id: Optional[str] = None,
    ) -> None:
        import config  # local import: avoids a hard dependency for callers that never touch Drive

        self._client_id = client_id or config.GOOGLE_DRIVE_CLIENT_ID
        self._client_secret = client_secret or config.GOOGLE_DRIVE_CLIENT_SECRET
        self._refresh_token = refresh_token or config.GOOGLE_DRIVE_REFRESH_TOKEN
        self._root_folder_id = root_folder_id if root_folder_id is not None else config.GOOGLE_DRIVE_ROOT_FOLDER_ID
        self._service = None

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Builds a refreshed Drive API client. Returns True on success."""
        try:
            creds = Credentials(
                token=None,
                refresh_token=self._refresh_token,
                token_uri=_TOKEN_URI,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
            creds.refresh(GoogleAuthRequest())
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
            return True
        except Exception as exc:
            log.error("Drive: connection failed — %s", exc)
            self._service = None
            return False

    def disconnect(self) -> None:
        """No persistent connection to close (each API call is its own
        HTTP request) — kept for symmetry with EmailOTPReader's shape."""
        self._service = None

    def __enter__(self) -> "DriveClient":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.disconnect()

    # ── Root folder ──────────────────────────────────────────────────────

    def _ensure_root_folder(self) -> Optional[str]:
        """Returns GOOGLE_DRIVE_ROOT_FOLDER_ID if configured, else creates
        a top-level "Datascraper Documents" folder once and returns its id.
        Callers should persist a returned auto-created id back into
        GOOGLE_DRIVE_ROOT_FOLDER_ID (both .env files) so this lookup only
        ever happens once in practice — but re-running it is harmless
        (just an extra folder if not persisted), never destructive."""
        if self._root_folder_id:
            return self._root_folder_id
        if self._service is None:
            return None
        try:
            resp = self._service.files().list(
                q=f"name='Datascraper Documents' and mimeType='{_FOLDER_MIMETYPE}' and trashed=false",
                fields="files(id)",
                spaces="drive",
            ).execute()
            files = resp.get("files") or []
            if files:
                self._root_folder_id = files[0]["id"]
                return self._root_folder_id
            folder = self._service.files().create(
                body={"name": "Datascraper Documents", "mimeType": _FOLDER_MIMETYPE},
                fields="id",
            ).execute()
            self._root_folder_id = folder["id"]
            log.info("Drive: created root folder 'Datascraper Documents' (id=%s) — "
                     "consider saving this as GOOGLE_DRIVE_ROOT_FOLDER_ID in both .env files.",
                     self._root_folder_id)
            return self._root_folder_id
        except HttpError as exc:
            log.error("Drive: could not find/create root folder — %s", exc)
            return None

    # ── Per-app_no folder ────────────────────────────────────────────────

    def get_or_create_app_folder(self, app_no: str, cached_folder_id: Optional[str] = None) -> Optional[str]:
        """Returns cached_folder_id immediately if given (the common case —
        callers should pass my_trademarks.drive_folder_id here so this is a
        single indexed column read, not a Drive API call, on every upload
        after the first for a given app_no). Otherwise looks up-or-creates
        a folder named after app_no under the root folder. Returns None on
        failure (not connected, or a Drive API error) — callers should
        treat that as "skip this upload, local file stays," not a crash."""
        if cached_folder_id:
            return cached_folder_id
        if self._service is None:
            return None
        root_id = self._ensure_root_folder()
        if not root_id:
            return None
        try:
            resp = self._service.files().list(
                q=f"name='{app_no}' and '{root_id}' in parents and mimeType='{_FOLDER_MIMETYPE}' and trashed=false",
                fields="files(id)",
                spaces="drive",
            ).execute()
            files = resp.get("files") or []
            if files:
                return files[0]["id"]
            folder = self._service.files().create(
                body={"name": app_no, "mimeType": _FOLDER_MIMETYPE, "parents": [root_id]},
                fields="id",
            ).execute()
            return folder["id"]
        except HttpError as exc:
            log.error("Drive: could not find/create folder for app_no=%s — %s", app_no, exc)
            return None

    # ── Upload / download ────────────────────────────────────────────────

    def upload_bytes(self, data: bytes, filename: str, folder_id: str, mimetype: str) -> Optional[str]:
        """Uploads *data* as a new file named *filename* inside *folder_id*.
        Returns the new Drive file id, or None on failure (logged, never
        raised) — callers should treat a None return as "upload failed,
        leave the local file in place," never as a reason to delete
        anything."""
        if self._service is None:
            log.error("Drive: upload_bytes called with no active connection")
            return None
        try:
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
            file = self._service.files().create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media,
                fields="id",
            ).execute()
            return file.get("id")
        except HttpError as exc:
            log.error("Drive: upload failed for %s — %s", filename, exc)
            return None

    def download_bytes(self, file_id: str) -> Optional[bytes]:
        """Full download into memory — file sizes here are tens of MB at
        most, well within reason for a single in-memory buffer rather than
        chunked streaming. Returns None on failure (logged, never raised)."""
        if self._service is None:
            log.error("Drive: download_bytes called with no active connection")
            return None
        try:
            request = self._service.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
            return buf.getvalue()
        except HttpError as exc:
            log.error("Drive: download failed for file_id=%s — %s", file_id, exc)
            return None

    def get_file_metadata(self, file_id: str) -> Optional[dict]:
        """Returns {"size": str, "md5Checksum": str, "mimeType": str} or
        None on failure. Used by the standalone cleanup script to verify a
        Drive copy is intact before ever deleting the local original."""
        if self._service is None:
            log.error("Drive: get_file_metadata called with no active connection")
            return None
        try:
            return self._service.files().get(fileId=file_id, fields="size,md5Checksum,mimeType").execute()
        except HttpError as exc:
            log.error("Drive: metadata fetch failed for file_id=%s — %s", file_id, exc)
            return None
