"""Apply staging grants, verify preserved data and add loopback-only UI routing."""
import json
from pathlib import Path
import subprocess
import urllib.request

from prepare_server_staging import DB, RELEASE, STATE, fingerprints, psql, read_env


EXTRA_GRANTS = """
GRANT SELECT,INSERT,UPDATE,DELETE ON public.tm_leads,public.tm_lead_activities,
 public.tm_consultants,public.tm_packages,public.tm_system_settings,
 public.wordmark_searches,public.wordmark_search_results,public.wordmark_excel_jobs,
 public.trademarks TO tm_stage_user;
DO $$ DECLARE item record; BEGIN
 FOR item IN SELECT seq.relname FROM pg_class seq JOIN pg_depend d ON d.objid=seq.oid
 JOIN pg_class tbl ON tbl.oid=d.refobjid JOIN pg_namespace ns ON ns.oid=seq.relnamespace
 WHERE seq.relkind='S' AND ns.nspname='public' AND d.deptype IN ('a','i')
 AND tbl.relname IN ('tm_leads','tm_lead_activities','wordmark_searches','wordmark_search_results','trademarks')
 LOOP EXECUTE format('GRANT USAGE,SELECT ON SEQUENCE public.%I TO tm_stage_user',item.relname); END LOOP;
END $$;
"""
def main():
    expected = json.loads((STATE.parent / 'staging-before-fingerprints.json').read_text())
    actual = fingerprints()
    if expected != actual:
        raise RuntimeError('Staging migration changed original records; do not start the new services.')
    migrations = int(psql(DB, 'SELECT count(*) FROM public.fleet_schema_migrations;'))
    if migrations != len(list((RELEASE / 'shared-scraper/migrations').glob('[0-9]*.sql'))):
        raise RuntimeError('Unexpected migration count; stop for review.')
    grants = (RELEASE / 'deploy/fleet-service-grants.sql').read_text().replace(
        'tm_user_service', 'tm_stage_user').replace('tm_control_service', 'tm_stage_control')
    psql(DB, 'BEGIN;\n' + grants + EXTRA_GRANTS + '\nCOMMIT;')
    print('All original staging row fingerprints preserved; runtime grants applied.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Staging validation stopped:', str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise SystemExit(1)
