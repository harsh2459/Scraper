"""Bounded service checks. Does not display private tokens or DB URLs."""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from prepare_server_staging import STATE, read_env


def check_user_api(opener):
    try:
        with opener.open('http://127.0.0.1:5505/api/me', timeout=3) as response:
            body = json.load(response)
            if body.get('auth') is not False:
                raise ValueError('Unexpected anonymous user response.')
    except urllib.error.HTTPError as response:
        with response:
            if response.code != 401:
                raise
            body = json.load(response)
        if body.get('error') != 'auth required' or body.get('login') != '/login':
            raise ValueError('Unexpected user authentication response.')


def main():
    env = read_env(STATE.parent / 'staging-control.env')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    failure = 'Services did not respond.'
    for attempt in range(30):
        try:
            request = urllib.request.Request('http://127.0.0.1:5510/api/control/v1/admin/overview',
                       headers={'Authorization': 'Bearer ' + env['CONTROL_ADMIN_TOKEN']})
            with opener.open(request, timeout=3) as response:
                json.load(response)
            check_user_api(opener)
            print('Coordinator authenticated DB overview: PASS')
            print('User API anonymous authentication boundary: PASS')
            print('Staging uses a separate database; live Nginx routes remain unchanged.')
            return
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            failure = ('HTTP ' + str(exc.code)) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            time.sleep(2)
    raise SystemExit('Staging health failed (' + failure + '). Keep the current live deployment; inspect container status.')


if __name__ == '__main__':
    main()
