"""Exercise the running local fixture demo; never prints credentials."""
import json
from pathlib import Path
import time

import requests

runtime = Path(__file__).resolve().parents[2] / '.runtime'
config = dict(line.split('=', 1) for line in (runtime / 'fleet-demo.env').read_text().splitlines() if '=' in line)
if config.get('WORKER_MODE') != 'fixture' or config.get('CONTROL_ENV') != 'development':
    raise SystemExit('This check only supports the isolated fixture demo.')
base = 'http://127.0.0.1:5185'
session = requests.Session()
reply = session.post(base + '/api/login', json={'email': config['DEMO_ADMIN_EMAIL'], 'password': config['DEMO_ADMIN_PASSWORD']}, timeout=5)
reply.raise_for_status()
reply = session.post(base + '/api/add', json={'app_no': '9000001'}, timeout=5)
reply.raise_for_status()
control_headers = {'Authorization': 'Bearer ' + config['CONTROL_ADMIN_TOKEN']}
requests.post(base + '/api/control/v1/admin/dispatch', headers=control_headers, json={}, timeout=5).raise_for_status()
worker_headers = {'Authorization': 'Bearer ' + config['WORKER_LOCAL_TOKEN']}
response = requests.post('http://127.0.0.1:5011/api/worker/cycle', headers=worker_headers, json={}, timeout=5)
if response.status_code not in (202, 409):
    response.raise_for_status()
for _ in range(45):
    results = requests.get(base + '/api/control/v1/admin/results', headers=control_headers, timeout=5).json()['items']
    result = next((r for r in results if r['app_no'] == '9000001' and r['disposition'] == 'accepted'), None)
    if result:
        break
    time.sleep(1)
else:
    raise RuntimeError('Fixture worker did not deliver the requested result.')
print('HTTP demo: authenticated user addition -> fleet delivery -> accepted result passed.')

if __name__ == '__main__':
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp('http://127.0.0.1:9224')
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        context.add_init_script('sessionStorage.setItem("fleet-control-credential", ' + json.dumps(config['CONTROL_ADMIN_TOKEN']) + ');'
                                'sessionStorage.setItem("worker-local-credential", ' + json.dumps(config['WORKER_LOCAL_TOKEN']) + ');')
        page.goto(base + '/admin/')
        page.get_by_text('Fixture only', exact=True).count()  # No dependency on a specific portal record.
        page.locator('.ops-stat strong').first.wait_for()
        page.wait_for_function("document.querySelector('.ops-stat strong').textContent !== '0'")
        page.screenshot(path=str(runtime / 'admin-overview.png'), full_page=True)
        for title in ['Fleet machines', 'Portal accounts', 'Work queue', 'TM schedules', 'Refresh policies', 'Results & documents', 'Audit log']:
            page.get_by_role('button', name=title, exact=True).click()
            page.locator('.ops-table-card').wait_for()
            page.wait_for_timeout(250)
            if page.locator('[role="alert"]').count():
                raise AssertionError('Administration page failed: ' + title)
        page.get_by_role('button', name='Users', exact=True).click()
        page.get_by_placeholder('Administrator email').fill(config['DEMO_ADMIN_EMAIL'])
        page.get_by_placeholder('Password', exact=True).fill(config['DEMO_ADMIN_PASSWORD'])
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.get_by_text('Signed in as ' + config['DEMO_ADMIN_EMAIL']).wait_for()
        page.screenshot(path=str(runtime / 'admin-users.png'), full_page=True)
        page.goto('http://127.0.0.1:5011/')
        page.locator('.ops-stat strong').first.wait_for()
        page.screenshot(path=str(runtime / 'worker-overview.png'), full_page=True)
        page.get_by_role('button', name='Assignments & results', exact=True).click()
        page.get_by_text('9000001', exact=True).wait_for()
        page.screenshot(path=str(runtime / 'worker-assignments.png'), full_page=True)
        if errors:
            raise AssertionError('Browser errors: ' + '; '.join(errors))
        context.close()
        browser.close()
    print('Browser check: admin pages, user administration, worker results passed; screenshots saved in .runtime.')
