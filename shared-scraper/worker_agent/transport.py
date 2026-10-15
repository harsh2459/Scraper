import requests


class CoordinatorClient:
    def __init__(self, settings):
        self.base = settings.coordinator_url + '/api/control/v1/worker'
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['Authorization'] = 'Bearer ' + settings.worker_token
        self.before_request = None

    def request(self, method, path, **kwargs):
        if self.before_request:
            self.before_request()
        timeout = kwargs.pop('timeout', (5, 60))
        response = self.session.request(method, self.base + path, timeout=timeout, allow_redirects=False, **kwargs)
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise RuntimeError('Coordinator redirects are not supported.')
        return response.json()

    def health(self):
        if self.before_request:
            self.before_request()
        response = self.session.get(self.base.rsplit('/worker', 1)[0] + '/health', timeout=(3, 10), allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200 or response.json().get('status') != 'ok':
            raise RuntimeError('Coordinator health check failed.')

    def heartbeat(self, phase, metadata):
        return self.request('POST', '/heartbeat', json={'phase': phase, 'metadata': metadata}, timeout=(3, 10))

    def reconcile(self):
        return self.request('GET', '/reconcile')['assignments']

    def configuration(self):
        return self.request('GET', '/configuration')

    def repair(self, result_id):
        return self.request('POST', '/repair', json={'result_id': result_id})

    def claim(self, limit, repair_only=False):
        return self.request('POST', '/claim', json={'limit': limit, 'repair_only': repair_only})['assignments']

    def offline(self, jobs):
        return self.request('POST', '/offline', json={'assignments': [{'id': j['id'], 'reservation_token': j['reservation_token']} for j in jobs]})

    def upload(self, result, artifact, content):
        return self.request('PUT', f"/artifacts/{result['result_id']}/{artifact['sha256']}", data=content,
                            headers={'Content-Type': 'application/octet-stream', 'X-Job-ID': result['job_id'],
                                     'X-Reservation-Token': result['reservation_token']})

    def complete(self, result):
        return self.request('POST', '/complete', json=result)

    def fail(self, assignment, error):
        return self.request('POST', '/fail', json={'job_id': assignment['id'], 'reservation_token': assignment['reservation_token'],
                                                 'error': error, 'retryable': True})
