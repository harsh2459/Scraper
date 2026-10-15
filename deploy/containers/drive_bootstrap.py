"""Verify the intended account, resolve a fleet folder and test real storage."""
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/opt/tmtracker')
from fleet_storage import DriveArchive


def main():
    options = {k: os.environ.get('GOOGLE_DRIVE_' + k.upper(), '') for k in ('client_id','client_secret','refresh_token','root_folder_id')}
    archive = DriveArchive(options)
    account = archive.service.about().get(fields='user(emailAddress)').execute()['user']['emailAddress']
    if account.casefold() != 'chatgptbmaemp369@gmail.com':
        raise RuntimeError('Drive authorization is not the selected account.')
    if sys.argv[1] == 'bootstrap':
        # Prefer an existing accessible configured folder, otherwise use a
        # dedicated fleet folder. Never reset historical file or folder IDs.
        valid = False
        try:
            meta = archive.service.files().get(fileId=archive.folder, fields='mimeType,trashed').execute()
            valid = meta.get('mimeType') == 'application/vnd.google-apps.folder' and not meta.get('trashed')
        except Exception:
            pass
        if not valid:
            folders = archive.service.files().list(q="name='TM Tracker Fleet Documents' and mimeType='application/vnd.google-apps.folder' and trashed=false", fields='files(id)').execute().get('files',[])
            archive.folder = folders[0]['id'] if folders else archive.service.files().create(body={'name':'TM Tracker Fleet Documents','mimeType':'application/vnd.google-apps.folder'}, fields='id').execute()['id']
        print(json.dumps({'account': account, 'root_folder_id': archive.folder}))
        return
    probe = Path(os.environ['CONTROL_ARTIFACT_DIR']) / '.drive-preflight-id'
    if not probe.exists():
        # Persist the generated ID before uploading; retries use that ID.
        with probe.open('x') as handle:
            handle.write(archive.allocate())
            handle.flush()
            os.fsync(handle.fileno())
    file_id = probe.read_text().strip()
    content = b'TM Tracker production Drive storage acceptance probe.\n'
    if archive.metadata(file_id) is None:
        archive.put(file_id, content, hashlib.sha256(content).hexdigest())
    metadata = archive.metadata(file_id)
    if not metadata or metadata.get('trashed') or metadata.get('md5Checksum') != hashlib.md5(content).hexdigest() or archive.get(file_id) != content:
        raise RuntimeError('Drive upload/download verification failed.')
    print('Correct Drive account, upload, checksum and download: PASS')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Drive verification failed. No historical files were changed.', file=sys.stderr)
        raise SystemExit(1)
