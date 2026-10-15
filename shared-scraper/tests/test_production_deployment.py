import importlib.util
from pathlib import Path
import unittest
import hashlib
import sys
import tempfile
import json
import os
import shutil
import subprocess
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[2]/'deploy/containers'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nginx = load('production_nginx')
guard = load('production_entrypoint')
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/containers'))
try:
    deployment = load('server_production')
finally:
    sys.path.pop(0)
SITE = '''# Keep both certificates and the existing API
server { listen 80; server_name mytmtracker.com www.mytmtracker.com;
 return 301 https://$host$request_uri; }
server {
 listen 443 ssl;
 server_name mytmtracker.com www.mytmtracker.com;
 ssl_certificate /etc/letsencrypt/live/mytmtracker.com/fullchain.pem;
 ssl_certificate_key /etc/letsencrypt/live/mytmtracker.com/privkey.pem;
 root /opt/tmtracker/tm_manager/frontend/dist;
 location /api/ { proxy_pass http://127.0.0.1:5005; }
 location / { try_files $uri $uri/ /index.html; }
 add_header X-Test "a{b};c";
}
server { listen 443 ssl; server_name scraper.mytmtracker.com;
 location / { proxy_pass http://127.0.0.1:5001; } }
'''


class ProductionTests(unittest.TestCase):
    def test_activation_failure_restores_old_services(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'site.conf'
            target.write_text(SITE)
            state = {'phase':'prepared','nginx':str(target),'nginx_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
            with patch.object(deployment,'run',side_effect=[None,RuntimeError('stop failed')]),patch.object(deployment,'compose'), \
                 patch.object(deployment,'save'),patch.object(deployment,'rollback') as restore:
                with self.assertRaises(RuntimeError):
                    deployment.activate(state)
                restore.assert_called_once_with(state)
            self.assertEqual(target.read_text(),SITE)

    def test_rolled_back_deployment_can_retry_after_compose_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'site.conf'
            target.write_text(SITE)
            state = {'phase':'rolled-back','nginx':str(target),'nginx_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
            with patch.object(deployment,'run',side_effect=[None,RuntimeError('stop failed')]),patch.object(deployment,'compose'), \
                 patch.object(deployment,'save'),patch.object(deployment,'rollback') as restore:
                with self.assertRaisesRegex(RuntimeError,'stop failed'):
                    deployment.activate(state)
                restore.assert_called_once_with(state)

    @unittest.skipUnless(shutil.which('docker'),'Docker Compose CLI required')
    def test_rendered_compose_has_one_absolute_tmpfs_mount_per_service(self):
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ,'RELEASE_TAG':'test-validation','TMTRACKER_UID':'10001','TMTRACKER_GID':'10001'}
        result = subprocess.run(['docker','compose','-f',str(root/'deploy/containers/compose.production.yaml'),
                                 'config','--no-env-resolution','--format','json'],capture_output=True,text=True,env=env,check=True)
        configuration = json.loads(result.stdout)
        for service in configuration['services'].values():
            self.assertEqual(service['tmpfs'],['/tmp:size=256m,mode=1777'])

    def test_concurrent_nginx_changes_stop_before_service_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'site.conf'
            target.write_text(SITE)
            state = {'phase':'prepared','nginx':str(target),'nginx_sha256':'old-hash'}
            with patch.object(deployment,'run') as command,patch.object(deployment,'rollback') as restore:
                with self.assertRaises(RuntimeError):
                    deployment.activate(state)
                command.assert_not_called()
                restore.assert_not_called()

    def test_cutover_preserves_tls_other_hosts_and_existing_api(self):
        output = nginx.update(SITE,'/var/www/tmtracker-live/live-123')
        self.assertIn('root /var/www/tmtracker-live/live-123;',output)
        for line in SITE.splitlines():
            if ' root ' not in line:
                self.assertIn(line,output)
        self.assertIn('location ^~ /api/control/v1/admin/',output)
        self.assertIn('proxy_set_header Host $http_host;',output)
        self.assertEqual(len(nginx.servers(output)),3)

    def test_ambiguous_changed_root_and_existing_fleet_routes_are_refused(self):
        for value in (SITE+SITE,SITE.replace('/opt/tmtracker/tm_manager/frontend/dist','/other/app'),
                      SITE.replace('location /api/', 'location /admin/')):
            with self.assertRaises(ValueError):
                nginx.update(value,'/var/www/tmtracker-live/live-123')
        with self.assertRaises(ValueError):
            nginx.update(SITE,'/tmp/injected;')

    def test_production_never_accepts_staging_or_owner_roles(self):
        env = {'CONTROL_DATABASE_URL':'postgresql://tm_live_control@127.0.0.1/datascraper','CONTROL_STORAGE_MODE':'drive'}
        guard.validate('control',env)
        for url in ('postgresql://postgres@127.0.0.1/datascraper',
                    'postgresql://tm_live_control@127.0.0.1/fleet_staging_datascraper',
                    'postgresql://tm_live_control@62.146.239.158/datascraper'):
            with self.assertRaises(ValueError):
                guard.validate('control',{**env,'CONTROL_DATABASE_URL':url})
        guard.validate('control',{**env,'CONTROL_STORAGE_MODE':'local'})
        with self.assertRaises(ValueError):
            guard.validate('drive-preflight',{**env,'CONTROL_STORAGE_MODE':'local'})
        user = {'TM_DATABASE_URL':'postgresql://tm_live_user@127.0.0.1/datascraper','TM_AUTO_MIGRATE':'0',
                'TM_BIND_HOST':'127.0.0.1','COOKIE_SECURE':'1','SECRET_KEY':'test-only'}
        guard.validate('user',user)
        with self.assertRaises(ValueError):
            guard.validate('user',{**user,'TM_AUTO_MIGRATE':'1'})
