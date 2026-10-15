"""Read-only routing/DNS audit; HTTPS probes require an explicit --connected."""
import argparse
import json
from pathlib import Path

from .config import WorkerSettings
from .network import WorkerNetwork


def main(argv=None):
    parser = argparse.ArgumentParser(description='Audit worker routing without installing or starting a tunnel.')
    parser.add_argument('--vpn-config', required=True, help='Reviewed config name, without .conf')
    parser.add_argument('--coordinator-url', required=True, help='Actual coordinator HTTPS base URL')
    parser.add_argument('--portal-hosts', default='tmrsearch.ipindia.gov.in', help='Comma-separated reviewed portal hosts')
    parser.add_argument('--connected', action='store_true', help='Verify routes and HTTPS through an already running tunnel')
    parser.add_argument('--config-dir', type=Path, help='Directory containing the separately prepared private worker config')
    args = parser.parse_args(argv)
    probe_stage = 'configuration'
    try:
        settings = WorkerSettings(args.coordinator_url.rstrip('/'), 'routing-check-no-worker-credential',
                                  'routing-check-no-local-credential', Path('.'), mode='live',
                                  vpn_config=args.vpn_config, network_mode='split',
                                  portal_hosts=tuple(h.strip().lower() for h in args.portal_hosts.split(',') if h.strip()),
                                  vpn_config_dir=args.config_dir.resolve() if args.config_dir else None)
        guard = WorkerNetwork(settings)
        guard.preflight()
        if args.connected:
            import requests
            probe_stage = 'portal-route'
            guard.portal_check()
            probe_stage = 'coordinator-route'
            guard.coordinator_check()
            with requests.Session() as session:
                guard.install(session)
                # No login, OTP, account credentials or response bodies are read.
                probe_stage = 'portal-https'
                with session.get('https://' + settings.portal_hosts[0] + '/estatus', timeout=(3, 10), stream=True) as response:
                    response.raise_for_status()
            with requests.Session() as session:
                probe_stage = 'coordinator-https'
                session.trust_env = False
                response = session.get(settings.coordinator_url + '/api/control/v1/health',
                                       timeout=(3, 10), allow_redirects=False)
                if response.status_code != 200 or response.json().get('status') != 'ok':
                    raise RuntimeError('Coordinator HTTPS health probe failed.')
            guard.report(effective_mode='split', coordinator='verified', reason=None)
        print(json.dumps(guard.snapshot(), indent=2))
        return 0
    except Exception as exc:
        state = guard.snapshot() if 'guard' in locals() else {}
        # Requests exceptions can contain redirected URLs, so never print them.
        state.update(error='Routing check failed. Review config/DNS, tunnel routes and coordinator HTTPS.')
        state.update(failure_stage=probe_stage, error_type=type(exc).__name__)
        response = getattr(exc, 'response', None)
        if response is not None and isinstance(getattr(response,'status_code',None),int):
            state['http_status'] = response.status_code
        print(json.dumps(state, indent=2))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
