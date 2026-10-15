"""Idempotent document/proceeding facts and per-field provenance."""
from psycopg.types.json import Jsonb
from document_identity import source_key, metadata_fingerprint
from compliance_facts import parse_date
from .contracts import fingerprint


def flatten(fields, prefix=''):
    for name, value in fields.items():
        key = prefix + name
        if isinstance(value, dict):
            yield from flatten(value, key + '.')
        else:
            yield key, value


def canonical_fields(conn, app_no):
    fields = {}
    for row in conn.execute('SELECT field_key,value FROM field_values WHERE app_no=%s', (app_no,)).fetchall():
        parts = row['field_key'].split('.')
        target = fields
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = row['value']
    return fields


def document_outcomes(body):
    if 'documents' in body:
        return body['documents']
    # Version-one workers remain compatible; content identity deduplicates their
    # application forms even before they supply stable document metadata.
    fields = {item['sha256']: item['fields'] for item in body['extracted']}
    outcomes = []
    for artifact in body['artifacts']:
        if artifact['kind'] not in ('document', 'reminder', 'correspondence'):
            continue
        metadata = artifact.get('metadata') or {}
        key = source_key(artifact['kind'], metadata) if metadata else 'content:' + artifact['sha256']
        outcomes.append({'source_key': key, 'kind': artifact['kind'], 'metadata': metadata,
                         'metadata_fingerprint': metadata_fingerprint(metadata), 'sha256': artifact['sha256'],
                         'filename': artifact['filename'], 'download_state': 'downloaded',
                         'parse_state': 'parsed' if fields.get(artifact['sha256']) else 'review_needed',
                         'fields': fields.get(artifact['sha256']), 'events': []})
    return outcomes


def publish_facts(conn, body, worker_id):
    app_no, observed = body['app_no'], body['observed_at']
    for proceeding in body.get('proceedings', []):
        conn.execute('INSERT INTO proceedings(app_no,proceeding_key,portal_type,opposition_no,facts,observed_at) '
                     'VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(app_no,proceeding_key) DO UPDATE '
                     'SET facts=EXCLUDED.facts,observed_at=EXCLUDED.observed_at',
                     (app_no, proceeding['proceeding_key'], proceeding.get('portal_type'), proceeding['opposition_no'], Jsonb(proceeding), observed))
    for doc in document_outcomes(body):
        key = doc['source_key']
        previous = conn.execute('SELECT sha256,fields,legacy_id FROM document_ledger WHERE app_no=%s AND source_key=%s', (app_no, key)).fetchone()
        downloaded = doc['download_state'] == 'downloaded'
        conn.execute('INSERT INTO document_ledger(app_no,source_key,metadata,metadata_fingerprint,sha256,worker_id,result_id,filename,kind,'
                     'download_state,parse_state,fields,observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                     'ON CONFLICT(app_no,source_key) DO UPDATE SET metadata=EXCLUDED.metadata,metadata_fingerprint=EXCLUDED.metadata_fingerprint,'
                     'sha256=COALESCE(EXCLUDED.sha256,document_ledger.sha256),worker_id=COALESCE(EXCLUDED.worker_id,document_ledger.worker_id),'
                     'result_id=COALESCE(EXCLUDED.result_id,document_ledger.result_id),filename=COALESCE(EXCLUDED.filename,document_ledger.filename),'
                     'download_state=EXCLUDED.download_state,parse_state=EXCLUDED.parse_state,fields=EXCLUDED.fields,observed_at=EXCLUDED.observed_at',
                     (app_no, key, Jsonb(doc['metadata']), metadata_fingerprint(doc['metadata']), doc.get('sha256'),
                      worker_id if downloaded else None, body['result_id'] if downloaded else None, doc.get('filename'), doc['kind'],
                      doc['download_state'], doc['parse_state'], Jsonb(doc.get('fields')), observed))
        # Unchanged bytes/fields never take precedence just because they were
        # fetched again later. Explicit changes from the same source can correct
        # a prior value; a failed parse does not clear unrelated good fields.
        fields = doc.get('fields')
        if fields and (not previous or previous['sha256'] != doc.get('sha256') or previous['fields'] != fields):
            for name, value in flatten(fields):
                if name.startswith('_'):
                    continue
                current = conn.execute('SELECT f.source_key,f.value,d.metadata FROM field_values f '
                                       'LEFT JOIN document_ledger d ON d.app_no=f.app_no AND d.source_key=f.source_key '
                                       'WHERE f.app_no=%s AND f.field_key=%s', (app_no, name)).fetchone()
                if current and current['source_key'] != key and not current['source_key'].startswith('legacy:'):
                    old_date = parse_date((current['metadata'] or {}).get('date') or (current['metadata'] or {}).get('corres_date'))
                    new_date = parse_date(doc['metadata'].get('date') or doc['metadata'].get('corres_date'))
                    # Older or undated sources cannot erase newer corrections.
                    # All alternative values remain on their source document.
                    if current['value'] is not None and old_date and (not new_date or new_date < old_date):
                        continue
                conn.execute('INSERT INTO field_values(app_no,field_key,value,source_key,observed_at,source_sha256,result_id,worker_id,parser_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                             'ON CONFLICT(app_no,field_key) DO UPDATE SET value=EXCLUDED.value,source_key=EXCLUDED.source_key,observed_at=EXCLUDED.observed_at,'
                             'source_sha256=EXCLUDED.source_sha256,result_id=EXCLUDED.result_id,worker_id=EXCLUDED.worker_id,parser_version=EXCLUDED.parser_version',
                             (app_no, name, Jsonb(value), key, observed, doc.get('sha256'), body['result_id'], worker_id, body.get('parser_version', '1')))
        if not downloaded or doc['parse_state'] == 'failed':
            continue
        events = doc.get('events', [])
        conn.execute('UPDATE compliance_events SET active=false WHERE app_no=%s AND source_key=%s', (app_no, key))
        proceeding = key.split(':', 2)
        proceeding_key = ':'.join(proceeding[:2]) if not key.startswith(('application:', 'content:')) else None
        opposition_no = proceeding[1] if proceeding_key else None
        for event in events:
            facts = {**(event.get('facts') or {}), 'source_sha256': doc.get('sha256'),
                     'source_fingerprint': fingerprint({'sha256': doc.get('sha256'), 'event': event})}
            conn.execute('INSERT INTO compliance_events(app_no,source_key,event_type,category,proceeding_key,opposition_no,applies_to_side,'
                         'issue_date,event_date,due_date,due_basis,period_value,period_unit,anchor,needs_review,facts,observed_at) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                         'ON CONFLICT(app_no,source_key,event_type) DO UPDATE SET category=EXCLUDED.category,issue_date=EXCLUDED.issue_date,'
                         'event_date=EXCLUDED.event_date,due_date=EXCLUDED.due_date,due_basis=EXCLUDED.due_basis,period_value=EXCLUDED.period_value,'
                         'period_unit=EXCLUDED.period_unit,anchor=EXCLUDED.anchor,needs_review=EXCLUDED.needs_review,facts=EXCLUDED.facts,'
                         'active=true,observed_at=EXCLUDED.observed_at',
                         (app_no, key, event['event_type'], event['category'], proceeding_key, opposition_no, event.get('applies_to_side', 'own_mark'),
                          event.get('issue_date'), event.get('event_date'), event.get('due_date'), event.get('due_basis', 'unknown'),
                          event.get('period_value'), event.get('period_unit'), event.get('anchor'), event.get('needs_review', True), Jsonb(facts), observed))
