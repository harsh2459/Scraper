from dataclasses import replace
import hashlib
import unittest
import uuid
from unittest.mock import patch

from control_plane.app import create_app
from control_plane.repository import Repository, Conflict
from fleet_storage import archive_upload, artifact_bytes, StorageUnavailable
from tests import test_fleet as fleet


class FakeArchive:
    def __init__(self):
        self.files = {}
        self.allocations = 0
        self.lose_reply = False
        self.corrupt = False

    def allocate(self):
        self.allocations += 1
        return 'test-file-'+str(self.allocations)

    def metadata(self,file_id):
        if file_id not in self.files:
            return None
        content = self.files[file_id]
        return {'size':str(len(content)),'md5Checksum':'bad' if self.corrupt else hashlib.md5(content).hexdigest(),'trashed':False}

    def put(self,file_id,content,sha):
        self.files[file_id] = content
        if self.lose_reply:
            self.lose_reply = False
            raise ConnectionError('lost upload reply with private provider details')

    def get(self,file_id):
        return self.files[file_id]


class DriveStorageTests(unittest.TestCase):
    setUpClass = classmethod(fleet.FleetTests.setUpClass.__func__)
    setUp = fleet.FleetTests.setUp
    reserve = fleet.FleetTests.reserve
    body = fleet.FleetTests.body

    def artifact(self):
        content = b'original document bytes'
        result_id = str(uuid.uuid4())
        sha = hashlib.sha256(content).hexdigest()
        self.repo.record_artifact(self.worker['id'],result_id,sha,len(content))
        return result_id,sha,content

    def test_lost_upload_reply_reuses_saved_id_and_downloads_without_local_copy(self):
        result_id,sha,content = self.artifact()
        archive = FakeArchive()
        archive.lose_reply = True
        with self.assertRaises(StorageUnavailable) as error:
            archive_upload(self.repo,self.worker['id'],result_id,sha,content,archive)
        self.assertNotIn('private provider',str(error.exception))
        archive_upload(self.repo,self.worker['id'],result_id,sha,content,archive)
        archive_upload(self.repo,self.worker['id'],result_id,sha,content,archive)
        self.assertEqual(archive.allocations,1)
        self.assertEqual(len(archive.files),1)
        with self.repo.transaction() as conn, patch('fleet_storage.DriveArchive',return_value=archive):
            downloaded = artifact_bytes(conn,{'worker_id':self.worker['id'],'result_id':result_id,'sha256':sha},self.root,{})
        self.assertEqual(downloaded,content)

    def test_checksum_failure_never_marks_upload_verified(self):
        result_id,sha,content = self.artifact()
        archive = FakeArchive()
        archive.corrupt = True
        with self.assertRaises(StorageUnavailable):
            archive_upload(self.repo,self.worker['id'],result_id,sha,content,archive)
        with self.repo.transaction() as conn:
            self.assertIsNone(conn.execute('SELECT verified_at FROM artifact_storage').fetchone()['verified_at'])

    def test_completion_requires_drive_verification_and_retries_keep_local_bytes(self):
        settings = replace(self.settings,storage_mode='drive',drive_options={k:'test-only' for k in ('client_id','client_secret','refresh_token','root_folder_id')})
        self.repo = Repository(settings)
        app = create_app(settings)
        job = self.reserve()
        result_id = str(uuid.uuid4())
        content = b'reserved worker document'
        sha = hashlib.sha256(content).hexdigest()
        client = app.test_client()
        archive = FakeArchive()
        archive.lose_reply = True
        headers = {'Authorization':'Bearer '+self.worker['token'],'X-Job-ID':job['id'],'X-Reservation-Token':job['reservation_token']}
        endpoint = f'/api/control/v1/worker/artifacts/{result_id}/{sha}'
        with patch('control_plane.app.DriveArchive',return_value=archive):
            self.assertEqual(client.put(endpoint,data=content,headers=headers).status_code,503)
            path = settings.artifact_dir/self.worker['id']/result_id/sha
            self.assertEqual(path.read_bytes(),content)
            body = self.body(job,[{'sha256':sha,'size':len(content),'filename':'document.pdf','kind':'document'}])
            body['result_id'] = result_id
            with self.assertRaises(Conflict):
                self.repo.complete(self.worker['id'],body)
            self.assertEqual(client.put(endpoint,data=content,headers=headers).status_code,200)
            self.assertTrue(self.repo.complete(self.worker['id'],body)['acknowledged'])
