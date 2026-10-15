"""Verified Drive artifact storage. Network calls never run under DB row locks."""
import hashlib
import io
from pathlib import Path


class StorageUnavailable(RuntimeError):
    pass


class DriveArchive:
    def __init__(self, options):
        from google.oauth2.credentials import Credentials
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build
        import httplib2
        if not all(options.get(key) for key in ('client_id', 'client_secret', 'refresh_token', 'root_folder_id')):
            raise StorageUnavailable('Drive credentials and folder must be configured.')
        credentials = Credentials(token=None, token_uri='https://oauth2.googleapis.com/token',
                                  client_id=options['client_id'], client_secret=options['client_secret'],
                                  refresh_token=options['refresh_token'])
        self.service = build('drive', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)
        self.folder = options['root_folder_id']

    def allocate(self):
        return self.service.files().generateIds(count=1, space='drive').execute()['ids'][0]

    def metadata(self, file_id):
        from googleapiclient.errors import HttpError
        try:
            return self.service.files().get(fileId=file_id, fields='size,md5Checksum,trashed').execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise

    def put(self, file_id, content, sha):
        from googleapiclient.http import MediaIoBaseUpload
        from googleapiclient.errors import HttpError
        try:
            self.service.files().create(body={'id': file_id, 'name': sha, 'parents': [self.folder]},
                media_body=MediaIoBaseUpload(io.BytesIO(content), mimetype='application/octet-stream', resumable=False), fields='id').execute()
        except HttpError as exc:
            # Preallocated IDs make successful-upload/lost-reply retries safe.
            if exc.resp.status != 409:
                raise

    def get(self, file_id, max_bytes=32*1024*1024):
        from googleapiclient.http import MediaIoBaseDownload
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, self.service.files().get_media(fileId=file_id), chunksize=1024*1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if buffer.tell() > max_bytes:
                raise StorageUnavailable('Drive document exceeds its allowed size.')
        return buffer.getvalue()


def archive_upload(repo, worker_id, result_id, sha, content, archive):
    try:
        with repo.transaction() as conn:
            row = conn.execute('SELECT drive_file_id FROM artifact_storage WHERE worker_id=%s AND result_id=%s AND sha256=%s',
                               (worker_id, result_id, sha)).fetchone()
        if not row:
            proposed = archive.allocate()
            with repo.transaction() as conn:
                conn.execute('INSERT INTO artifact_storage(worker_id,result_id,sha256,drive_file_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                             (worker_id, result_id, sha, proposed))
                row = conn.execute('SELECT drive_file_id FROM artifact_storage WHERE worker_id=%s AND result_id=%s AND sha256=%s',
                                   (worker_id, result_id, sha)).fetchone()
        file_id = row['drive_file_id']
        metadata = archive.metadata(file_id)
        if metadata is None:
            archive.put(file_id, content, sha)
            metadata = archive.metadata(file_id)
        md5 = hashlib.md5(content).hexdigest()
        if not metadata or metadata.get('trashed') or int(metadata.get('size', -1)) != len(content) or metadata.get('md5Checksum') != md5:
            raise StorageUnavailable('Drive checksum verification failed; local copy retained.')
        with repo.transaction() as conn:
            conn.execute('UPDATE artifact_storage SET md5_checksum=%s,verified_at=now() WHERE worker_id=%s AND result_id=%s AND sha256=%s',
                         (md5, worker_id, result_id, sha))
    except Exception as exc:
        # Provider errors can contain authorization data; never return/log them.
        raise StorageUnavailable('Drive upload unavailable; local copy retained. Retry delivery.') from None


def artifact_bytes(conn, artifact, root, options):
    sha = artifact['sha256']
    path = Path(root) / artifact['worker_id'] / artifact['result_id'] / sha
    if path.is_file():
        content = path.read_bytes()
    else:
        row = conn.execute('SELECT drive_file_id FROM fleet_control.artifact_storage WHERE worker_id=%s AND result_id=%s AND sha256=%s AND verified_at IS NOT NULL',
                           (artifact['worker_id'], artifact['result_id'], sha)).fetchone()
        if not row:
            raise StorageUnavailable('Document is temporarily unavailable.')
        try:
            content = DriveArchive(options).get(row['drive_file_id'])
        except Exception:
            raise StorageUnavailable('Drive document is temporarily unavailable.') from None
    if hashlib.sha256(content).hexdigest() != sha:
        raise StorageUnavailable('Document checksum failed.')
    return content
