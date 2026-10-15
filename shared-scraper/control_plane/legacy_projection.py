"""Transactional read-model compatibility for the existing portfolio UI.

This adapter writes only fetched-data columns; it never changes owners,
private notes, subscriptions or permissions. No legacy modules are imported.
"""
from pathlib import Path
from psycopg import sql
from psycopg.types.json import Jsonb
from .facts_projection import canonical_fields


def exists(conn, table):
    return conn.execute('SELECT to_regclass(%s) AS name', ('public.' + table,)).fetchone()['name'] is not None


def attach_cached(conn, app_no):
    if not exists(conn, 'my_trademarks'):
        return
    old = conn.execute('SELECT last_search_id,fetched_at FROM public.my_trademarks WHERE app_no=%s AND last_search_id IS NOT NULL '
                       'ORDER BY fetched_at DESC NULLS LAST LIMIT 1', (app_no,)).fetchone()
    if old:
        conn.execute("UPDATE public.my_trademarks SET status='fetched',last_search_id=%s,fetched_at=%s,last_error=NULL "
                     'WHERE app_no=%s AND last_search_id IS NULL', (old['last_search_id'], old['fetched_at'], app_no))


def publish(conn, body, worker_id, artifact_root):
    if not (exists(conn, 'ipindia_results') and exists(conn, 'my_trademarks')):
        raise RuntimeError('Legacy projection requires the user/scraper schema; run schema preparation first.')
    app_no, observed, data = body['app_no'], body['observed_at'], body['data']
    # An incomplete refresh updates matched fields and retains earlier fields
    # that were not observed. Explicit nulls remain explicit corrections.
    previous = conn.execute('SELECT r.data_json FROM public.my_trademarks t JOIN public.ipindia_results r ON r.id=t.last_search_id '
                            'WHERE t.app_no=%s ORDER BY t.fetched_at DESC NULLS LAST LIMIT 1', (app_no,)).fetchone()
    data = {**((previous['data_json'][0] or {}) if previous and previous['data_json'] else {}), **data}
    if data.get('Application Date') and not data.get('Date of Application'):
        data['Date of Application'] = data['Application Date']
    search_id = conn.execute("INSERT INTO public.ipindia_results(query,search_type,data_json,error,scraped_at) "
                             "VALUES(%s,'appno_batch',%s,'',%s) RETURNING id", (app_no, Jsonb([data]), observed)).fetchone()['id']
    if exists(conn, 'ipindia_rows'):
        for key, value in data.items():
            conn.execute('INSERT INTO public.ipindia_rows(search_id,field_key,field_val,row_index) VALUES(%s,%s,%s,0)',
                         (search_id, key, str(value) if value is not None else None))
    directory = artifact_root / worker_id / body['result_id']
    artifacts = {a['sha256']: a for a in body['artifacts']}
    if exists(conn, 'tma_documents'):
        columns = {r['column_name'] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                                        "WHERE table_schema='public' AND table_name='tma_documents'").fetchall()}
        items = list(body['extracted'])
        merged = canonical_fields(conn, app_no)
        if merged:
            # One stable compatibility row contains the merged field read model;
            # individual source rows remain available for inspection.
            source = next((a for a in body['artifacts'] if a['sha256'] in {i['sha256'] for i in items}), None)
            if source:
                items.append({'sha256': source['sha256'], 'fields': merged, 'canonical': True})
        for item in items:
            artifact = artifacts.get(item.get('sha256'))
            fields = item.get('fields') or {}
            if not artifact or not isinstance(fields, dict):
                continue
            row = {'app_no': app_no, 'search_id': search_id, 'source_pdf_filename': artifact['filename'],
                   'source_pdf_path': str(directory / artifact['sha256']), 'raw_json': Jsonb(fields), 'extracted_at': observed}
            row['fleet_canonical'] = bool(item.get('canonical'))
            row['fleet_sha256'] = None if item.get('canonical') else artifact['sha256']
            remote = conn.execute('SELECT drive_file_id,verified_at FROM artifact_storage WHERE worker_id=%s AND result_id=%s AND sha256=%s AND verified_at IS NOT NULL',
                                  (worker_id, body['result_id'], artifact['sha256'])).fetchone()
            if remote:
                row.update(drive_file_id=remote['drive_file_id'], drive_uploaded_at=remote['verified_at'])
            for name in ('form_type', 'application_date', 'registry_office', 'application_filed_as', 'use_statement',
                         'language', 'fee', 'temp_ref_no', 'receipt_no', 'receipt_date', 'receipt_amount',
                         'form_no', 'agent_code', 'proprietor_code', 'other_information'):
                if name in fields:
                    row[name] = str(fields[name]) if fields[name] is not None else None
            for group, mapping in {
                'applicant': {'name': 'applicant_name', 'address': 'applicant_address', 'mobile': 'applicant_mobile',
                              'email': 'applicant_email', 'country': 'applicant_country', 'partners': 'applicant_partners',
                              'address_for_service': 'applicant_address_for_service', 'jurisdiction': 'applicant_jurisdiction',
                              'nature': 'applicant_nature', 'legal_status': 'applicant_legal_status'},
                'agent': {'name': 'agent_name', 'address': 'agent_address', 'registration_no': 'agent_registration_no', 'nature': 'agent_nature'},
                'mark': {'trade_mark': 'mark_trade_mark', 'category': 'mark_category', 'image_description': 'mark_image_description'},
                'class': {'number': 'class_number', 'description': 'class_description'},
                'verification': {'date': 'verification_date', 'signed_for': 'verification_signed_for'},
            }.items():
                for key, column in mapping.items():
                    value = (fields.get(group) or {}).get(key)
                    if key in (fields.get(group) or {}):
                        row[column] = str(value) if value is not None else None
            row = {key: value for key, value in row.items() if key in columns}
            existing = conn.execute('SELECT id FROM public.tma_documents WHERE app_no=%s AND '
                                    '(CASE WHEN %s THEN fleet_canonical ELSE fleet_sha256=%s END) LIMIT 1',
                                    (app_no, bool(item.get('canonical')), artifact['sha256'])).fetchone()
            if existing:
                conn.execute(sql.SQL('UPDATE public.tma_documents SET {} WHERE id=%s').format(
                    sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in row)), [*row.values(), existing['id']])
            else:
                conn.execute(sql.SQL('INSERT INTO public.tma_documents ({}) VALUES ({})').format(
                    sql.SQL(',').join(map(sql.Identifier, row)), sql.SQL(',').join(sql.Placeholder() for _ in row)), list(row.values()))
    publication_artifacts = body['artifacts']
    if 'documents' in body:
        publication_artifacts = [a for a in publication_artifacts if a['kind'] not in ('reminder', 'correspondence')]
        publication_artifacts += [{'sha256': d['sha256'], 'filename': d['filename'], 'kind': d['kind'], 'metadata': d['metadata']}
                                 for d in body['documents'] if d['kind'] in ('reminder', 'correspondence') and d['download_state'] == 'downloaded']
    for artifact in publication_artifacts:
        path = directory / artifact['sha256']
        kind = artifact['kind']
        if kind in ('pr_details', 'app_details') and exists(conn, 'ipindia_page_fetches'):
            conn.execute('INSERT INTO public.ipindia_page_fetches(app_no,search_id,kind,raw_html,fetched_at) VALUES(%s,%s,%s,%s,%s) '
                         'ON CONFLICT(app_no,kind) DO UPDATE SET search_id=EXCLUDED.search_id,raw_html=EXCLUDED.raw_html,fetched_at=EXCLUDED.fetched_at',
                         (app_no, search_id, kind, path.read_text(encoding='utf-8', errors='replace'), observed))
        if kind == 'logo' and exists(conn, 'ipindia_trademark_images'):
            conn.execute('INSERT INTO public.ipindia_trademark_images(app_no,image_path,fetched_at) VALUES(%s,%s,%s) '
                         'ON CONFLICT(app_no) DO UPDATE SET image_path=EXCLUDED.image_path,fetched_at=EXCLUDED.fetched_at', (app_no, str(path), observed))
        table = {'reminder': 'ipindia_reminders', 'correspondence': 'ipindia_correspondence'}.get(kind)
        if table and exists(conn, table):
            metadata = artifact.get('metadata') or {}
            reference_column = 'corres_num' if kind == 'reminder' else 'view_url'
            columns = ['app_no', 'search_id', 's_no', 'corres_no', 'corres_date', 'subject',
                       'despatch_no', 'despatch_date', reference_column, 'document_path', 'fetched_at']
            values = [app_no, search_id, metadata.get('s_no'), metadata.get('corres_no') or None,
                      metadata.get('corres_date'), str(metadata.get('subject', artifact['filename'])),
                      metadata.get('despatch_no'), metadata.get('despatch_date'), metadata.get(reference_column),
                      artifact['filename'], observed]
            updates = [column for column in columns if column not in ('app_no', 'corres_no')]
            # Keep the source row ID stable so a user's compliance status
            # survives repeated observations of the same registry notice.
            prior = None
            if not metadata.get('corres_no') and metadata.get('source_key'):
                prior = conn.execute('SELECT legacy_id FROM document_ledger WHERE app_no=%s AND source_key=%s', (app_no, metadata['source_key'])).fetchone()
            if prior and prior['legacy_id']:
                conn.execute(sql.SQL('UPDATE public.{} SET {} WHERE id=%s').format(sql.Identifier(table),
                    sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(c)) for c in columns)), [*values, prior['legacy_id']])
                continue
            saved = conn.execute(sql.SQL('INSERT INTO public.{}({}) VALUES({}) ON CONFLICT(app_no,corres_no) DO UPDATE SET {} RETURNING id').format(
                sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)),
                sql.SQL(',').join(sql.Placeholder() for _ in columns),
                sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c), sql.Identifier(c)) for c in updates)), values).fetchone()
            if metadata.get('source_key'):
                conn.execute('UPDATE document_ledger SET legacy_id=%s WHERE app_no=%s AND source_key=%s', (saved['id'], app_no, metadata['source_key']))
    portfolios = conn.execute("UPDATE public.my_trademarks SET status='fetched',last_search_id=%s,fetched_at=%s,"
                              "job_id=NULL,last_error=%s,updated_at=now() WHERE app_no=%s RETURNING id",
                              (search_id, observed, 'Some documents need retry or review' if body['completeness'] == 'partial' else None, app_no)).fetchall()
    if exists(conn, 'tm_status_history'):
        fields = {'status': 'Status', 'sub_status': 'Sub Status', 'trade_mark': 'Trade Mark', 'proprietor_name': 'Proprietor Name',
                  'class_number': 'Class', 'valid_upto': 'Valid Upto/Renewed Upto', 'publication_details': 'Publication Details', 'tm_type': 'TM Type'}
        for portfolio in portfolios:
            previous = conn.execute('SELECT * FROM public.tm_status_history WHERE my_trademark_id=%s ORDER BY fetched_at DESC,id DESC LIMIT 1', (portfolio['id'],)).fetchone()
            snapshot = {name: str(data[label]) if data.get(label) is not None else None for name, label in fields.items()}
            changed = {name: {'from': previous[name], 'to': value} for name, value in snapshot.items() if previous and previous[name] != value}
            columns = ['my_trademark_id', 'app_no', 'search_id', 'fetched_at', *snapshot, 'changed_fields', 'is_first']
            conn.execute(sql.SQL('INSERT INTO public.tm_status_history({}) VALUES({})').format(
                sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns)),
                [portfolio['id'], app_no, search_id, observed, *snapshot.values(), Jsonb(changed), previous is None])
