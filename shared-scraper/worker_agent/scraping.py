"""Scraping adapters produce local bundles, never Contabo DB writes."""
import json
import re
import uuid

from control_plane.contracts import utcnow


class FixtureScraper:
    """Explicit demo/test adapter. Does not contact the portal or email."""
    def __init__(self, store):
        self.store = store

    def scrape(self, assignment):
        result_id = str(uuid.uuid4())
        data = {'Application No': assignment['app_no'], 'Trade Mark': 'DEVELOPMENT FIXTURE',
                'Status': 'Fixture only', 'Class': '35', 'Application Date': '01/01/2026'}
        artifact, path = self.store.save_artifact(result_id, 'fixture-details.json', json.dumps(data).encode(), 'document')
        return make_result(assignment, result_id, data, [artifact], [], [], 'complete'), {artifact['sha256']: path}


def make_result(assignment, result_id, data, artifacts, extracted, warnings, completeness):
    return {'schema_version': 1, 'result_id': result_id, 'job_id': assignment['id'],
            'reservation_token': assignment['reservation_token'], 'generation': assignment['generation'],
            'app_no': assignment['app_no'], 'observed_at': utcnow().isoformat(), 'data': data,
            'artifacts': artifacts, 'extracted': extracted, 'warnings': warnings, 'completeness': completeness}


class PortalScraper:
    def __init__(self, store, account_file, full_check_days=30, network=None):
        self.store = store
        self.account_file = account_file
        self.session = None
        self.account = None
        self.full_check_days = full_check_days
        self.network = network
        self.logged_in_at = 0.0
        # IP India ends a login session after roughly 25 minutes regardless of keepalive;
        # re-login before that, as the legacy warm pool did (WARM_POOL_MAX_SESSION_AGE_SECONDS).
        import os
        self.max_session_seconds = int(os.environ.get('WORKER_SESSION_MAX_SECONDS', '1200'))

    def _relogin(self):
        import logging
        logging.getLogger('worker_agent').info('Refreshing the portal login session')
        try:
            self.close()
        except Exception:
            self.session = None
        self.start()

    def start(self):
        # Imported only in explicit live mode. Legacy env loading and VPN
        # auto-rotation are disabled; this agent owns the VPN lifecycle.
        import os
        os.environ['SCRAPER_LOAD_LEGACY_ENV'] = '0'
        os.environ['SCRAPER_EXTERNAL_VPN_CONTROL'] = '1'
        from ipindia_scraper import authenticate_pool_account
        account = self.account or (json.loads(self.account_file.read_text(encoding='utf-8')) if self.account_file else {})
        for field in ('email_address', 'app_password', 'imap_server', 'imap_port'):
            if field not in account:
                raise ValueError('Local portal account configuration is incomplete.')
        import logging
        import re
        import time
        for attempt in range(1, 4):
            self.session, error = (authenticate_pool_account(account, http_guard=self.network) if self.network
                                   else authenticate_pool_account(account))
            if self.session:
                self.logged_in_at = time.monotonic()
                return
            # The portal rate-limits OTP requests ("wait N seconds") and its email can be late.
            # Waiting here keeps the VPN session instead of tearing everything down and redoing it.
            wait = re.search(r'wait (\d+) sec', error or '')
            if attempt == 3 or not (wait or 'OTP not received' in (error or '')):
                break
            pause = int(wait.group(1)) + 15 if wait else 95
            logging.getLogger('worker_agent').warning('Portal login attempt %d/3 failed (%s); retrying in %ds',
                                                      attempt, (error or '')[:120], pause)
            time.sleep(pause)
        raise RuntimeError(error or 'Portal authentication failed')

    def stop(self):
        if self.session:
            self.session.stop_keepalive()

    def close(self):
        if self.session:
            self.stop()
            try:
                self.session.logout()
            finally:
                self.session._http.close()
                self.session = None

    def scrape(self, assignment):
        import time
        if self.session and self.logged_in_at and time.monotonic() - self.logged_in_at > self.max_session_seconds:
            self._relogin()
        try:
            return self._scrape_once(assignment)
        except Exception:
            # A healthy session means this is a genuine failure for this application.
            # A dead one (expired or dropped) needs a fresh login and one retry.
            if self.session and self.session.ping_alive():
                raise
            self._relogin()
            return self._scrape_once(assignment)

    def _scrape_once(self, assignment):
        from ipindia_scraper import SearchOutcome
        from pdf_scraper.local_processor import extract_local
        from portal_formats import parse_opposition_rows, parse_opposition_record, parse_notice_table, require_portal_fragment, require_list_fragment, application_document_links
        from document_identity import source_key, metadata_fingerprint, meaningful, PARSER_VERSION
        from compliance_facts import document_events
        result_id = str(uuid.uuid4())
        outcome, data = self.session.search_appno_classified(assignment['app_no'])
        if outcome != SearchOutcome.HIT or not isinstance(data, dict):
            raise RuntimeError('Portal returned no usable record: ' + str(outcome))
        view = data.pop('_view_token', None)
        rvt = data.pop('_request_verification_token', None)
        raw = data.pop('_raw_html', None)
        observed = utcnow().isoformat()
        artifacts, extracted, warnings, paths = [], [], [], {}
        documents, proceedings, stages = [], [], {'core': {'state': 'complete', 'required': True}}
        force_full = assignment.get('force_full', False)
        local_key = assignment['id'] + ':' + assignment['reservation_token']

        def bundle(completeness):
            result = make_result(assignment, result_id, data, artifacts, extracted, warnings, completeness)
            result.update(observed_at=observed, documents=documents, proceedings=proceedings, stages=stages,
                          parser_version=PARSER_VERSION)
            return result

        def checkpoint():
            self.store.save_result(local_key, bundle('partial'), paths)

        def save(name, content, kind, metadata=None):
            artifact, path = self.store.save_artifact(result_id, name, content, kind)
            if metadata:
                artifact['metadata'] = metadata
            if artifact['sha256'] not in paths:
                artifacts.append(artifact)
                paths[artifact['sha256']] = path
            return artifact, path

        def process_document(kind, metadata, download, ordinal=1, proceeding='application'):
            key = source_key(kind, metadata, ordinal, proceeding)
            metadata = {**metadata, 'source_key': key}
            item = {'source_key': key, 'kind': kind, 'metadata': metadata,
                    'metadata_fingerprint': metadata_fingerprint(metadata), 'download_state': 'failed',
                    'parse_state': 'pending', 'events': []}
            documents.append(item)
            stages[key] = {'state': 'pending', 'required': True}
            checkpoint()
            try:
                cache = None if force_full else self.store.cached_document(assignment['app_no'], key, item['metadata_fingerprint'], self.full_check_days)
                if cache:
                    content = __import__('pathlib').Path(cache['path']).read_bytes()
                    artifact = {**cache['artifact'], 'metadata': metadata}
                    path = cache['path']
                    if artifact['sha256'] not in paths:
                        artifacts.append(artifact)
                        paths[artifact['sha256']] = path
                    fields, events = cache['fields'], cache['events']
                    # Reuse bytes, but retry failed/old extraction without portal traffic.
                    reparse = cache['parser_version'] != PARSER_VERSION or not fields
                else:
                    downloaded = download()
                    if not downloaded:
                        raise ValueError('Document download failed')
                    content, extension = downloaded if isinstance(downloaded, tuple) else (downloaded, 'pdf')
                    if not content.startswith(b'%PDF'):
                        require_portal_fragment(content.decode('utf-8', errors='replace'))
                    artifact, path = save(f"{kind}-{len(documents)}.{extension}", content, kind, metadata)
                    fields, events, reparse = None, [], True
                if reparse:
                    try:
                        if content.startswith(b'%PDF'):
                            fields, logo = extract_local(content)
                            if logo:
                                save(f"{kind}-{len(documents)}-logo.png", logo, 'logo')
                        events = document_events(content, metadata, proceeding if proceeding != 'application' else None)
                        item['parse_state'] = 'review_needed' if (fields or {}).get('_extraction', {}).get('needs_review') else 'parsed' if fields or events else 'review_needed'
                    except Exception:
                        item['parse_state'] = 'failed'
                        warnings.append('Document extraction needs retry: ' + key)
                else:
                    item['parse_state'] = 'review_needed' if (fields or {}).get('_extraction', {}).get('needs_review') else 'parsed' if fields or events else 'review_needed'
                item.update(sha256=artifact['sha256'], fields=fields, events=events, download_state='downloaded',
                            filename=artifact['filename'], reused=bool(cache), parser_version=PARSER_VERSION)
                if fields and not any(e['sha256'] == artifact['sha256'] for e in extracted):
                    extracted.append({'sha256': artifact['sha256'], 'fields': fields, 'source_key': key})
                if not cache or reparse:
                    # Re-parsing must not reset the safety-check clock.
                    self.store.cache_document(assignment['app_no'], key, item['metadata_fingerprint'], artifact, path,
                                              fields, events, PARSER_VERSION if item['parse_state'] != 'failed' else 'failed')
                    if cache:
                        with self.store.connect() as conn:
                            conn.execute('UPDATE document_cache SET checked_at=? WHERE app_no=? AND source_key=?',
                                         (cache['checked_at'], assignment['app_no'], key))
                stages[key]['state'] = 'complete'
            except Exception as exc:
                if type(exc).__name__ == 'PortalDocumentMissing':
                    # Dead portal link (404): retrying cannot succeed. Leave it out of
                    # the manifest and the required stages so the refresh can complete.
                    documents.remove(item)
                    stages[key] = {'state': 'complete', 'required': False}
                    warnings.append('Document unavailable on portal: ' + key)
                else:
                    stages[key]['state'] = 'failed'
                    warnings.append('Document download needs retry: ' + key)
            checkpoint()

        def notices(kind, rows, proceeding='application'):
            duplicates = {}
            for metadata in rows:
                signature = metadata_fingerprint(metadata)
                duplicates[signature] = duplicates.get(signature, 0) + 1
                def download(metadata=metadata):
                    if metadata.get('view_url'):
                        return self.session.download_correspondence_document(metadata['view_url'])
                    if metadata.get('corres_num'):
                        return self.session.download_reminder_document(metadata['corres_num'])
                    raise ValueError('Missing document locator')
                process_document(kind, metadata, download, duplicates[signature], proceeding)

        def stage(name, operation, required=True):
            stages[name] = {'state': 'pending', 'required': required}
            checkpoint()
            try:
                value = operation()
                stages[name]['state'] = 'complete'
                return value
            except Exception:
                stages[name]['state'] = 'failed'
                warnings.append('Stage needs retry: ' + name)
            finally:
                checkpoint()

        stages['panels'] = {'state': 'pending', 'required': True}
        checkpoint()
        if raw:
            save('status-page.html', str(raw).encode(), 'app_details')
        if not view:
            warnings.append('Missing view-details token; panels could not be fetched')
            stages['panels']['state'] = 'failed'
        else:
            def details():
                html = self.session.get_app_details(view, rvt)
                require_portal_fragment(html)
                save('app_details.html', html.encode(), 'app_details')
                # Preserve parsed public details alongside the raw source.
                try:
                    from ipindia_scraper import extract_app_details_fields
                    data['Application Details'] = extract_app_details_fields(html)
                except Exception:
                    pass
                return html
            html = stage('app_details', details)
            discovered = parse_opposition_rows(html or '')
            if 'opposed' in str(data.get('Status', '')).lower() and not discovered and not re.search(r'opposition|rectification', html or '', re.I):
                stages['opposition_discovery'] = {'state': 'failed', 'required': True}
                warnings.append('Opposed record has no recognized proceeding section')
            for metadata in application_document_links(html or ''):
                process_document('correspondence', metadata, lambda metadata=metadata: self.session.download_correspondence_document(metadata['view_url']))
            # Inspect every proceeding and its small lists even when Status is unchanged.
            for row in discovered:
                key = str(row.get('portal_type') or 'unknown') + ':' + row['opposition_no']
                proceeding = {**row, 'proceeding_key': key}
                proceedings.append(proceeding)
                def fetch_record(row=row, proceeding=proceeding, key=key):
                    record_html = self.session.get_proceeding_page(row['details_url'])
                    record = parse_opposition_record(record_html)
                    if not record.get('opposition_reference'):
                        raise ValueError('Unrecognized proceeding record')
                    proceeding['record'] = record
                    save(f"proceeding-{len(proceedings)}.html", record_html.encode(), 'proceeding', proceeding)
                    notices('opposition_correspondence', record['correspondence'], key)
                stage('proceeding:' + key, fetch_record)
                if row.get('reminders_url'):
                    def fetch_reminders(row=row, key=key):
                        notice_html = self.session.get_proceeding_page(row['reminders_url'])
                        require_list_fragment(notice_html)
                        notices('opposition_reminder', parse_notice_table(notice_html), key)
                    stage('proceeding_reminders:' + key, fetch_reminders)
            def uploads():
                duplicates = {}
                for doc in self.session.get_uploaded_documents(view, rvt):
                    metadata = {'description': doc.get('description'), 'date': doc.get('date'), 'portal_locator': doc.get('document_no')}
                    signature = metadata_fingerprint(metadata)
                    duplicates[signature] = duplicates.get(signature, 0) + 1
                    process_document('document', metadata, lambda doc=doc: self.session.download_document(doc['document_no']), duplicates[signature])
            stage('uploaded_list', uploads)
            for kind, getter in [('reminder', self.session.get_reminders), ('correspondence', self.session.get_correspondence)]:
                stage(kind + '_list', lambda kind=kind, getter=getter: notices(kind, getter(view, rvt)))
            signature = metadata_fingerprint({
                'core': {k: v for k, v in data.items() if k.lower() not in ('as on date', 'as on')},
                'documents': sorted([{'source_key': d['source_key'], 'metadata': meaningful(d['metadata'])} for d in documents], key=lambda d: d['source_key']),
                'proceedings': [{**meaningful({k: v for k, v in p.items() if k != 'record'}),
                                 'record': {k: v for k, v in (p.get('record') or {}).items() if k != 'correspondence'}} for p in proceedings]})
            old_signature = self.store.get('application-signature:' + assignment['app_no'])
            ancillary_key = 'application-ancillary:' + assignment['app_no']
            import time
            ancillary_due = time.time() - self.store.get(ancillary_key, 0) >= self.full_check_days * 86400
            if force_full or signature != old_signature or ancillary_due:
                def pr_details():
                    pr = self.session.get_pr_details(view, rvt)
                    require_portal_fragment(pr)
                    save('pr_details.html', pr.encode(), 'pr_details')
                stage('pr_details', pr_details, required=False)
                def logo():
                    image = self.session.get_trademark_image()
                    if image:
                        save('trademark-image.png', image, 'logo')
                stage('logo', logo, required=False)
                if stages['pr_details']['state'] == 'complete' and stages['logo']['state'] == 'complete':
                    self.store.set(ancillary_key, time.time())
                    self.store.set('application-signature:' + assignment['app_no'], signature)
            stages['panels']['state'] = 'complete'
        # Existing UI expects these key fields even when supplied only by PDF.
        for item in extracted:
            fields = item['fields']
            if not data.get('Class'):
                data['Class'] = (fields.get('class') or {}).get('number')
            if not data.get('Application Date'):
                data['Application Date'] = fields.get('application_date')
        core_complete = bool(data.get('Class') and (data.get('Application Date') or data.get('Date of Application')))
        stages['core']['state'] = 'complete' if core_complete else 'failed'
        complete = core_complete and all(s['state'] == 'complete' for s in stages.values() if s['required'])
        return bundle('complete' if complete else 'partial'), paths
