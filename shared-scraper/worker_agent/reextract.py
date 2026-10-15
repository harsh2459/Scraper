"""Re-extract saved artifacts under a new reservation; no portal or VPN."""
import copy
from pathlib import Path
import uuid
from control_plane.contracts import digest, utcnow
from compliance_facts import document_events
from document_identity import PARSER_VERSION, source_key, metadata_fingerprint
from .scraping import make_result


def reextract(store, assignment):
    from pdf_scraper.local_processor import extract_local
    original, paths = store.result_by_id(assignment['repair_result_id'])
    artifacts = copy.deepcopy(original['artifacts'])
    documents = copy.deepcopy(original.get('documents', []))
    extracted, warnings = [], []
    for artifact in artifacts:
        content = Path(paths[artifact['sha256']]).read_bytes()
        if digest(content) != artifact['sha256']:
            raise ValueError('Saved artifact checksum failed')
        if artifact['kind'] not in ('document', 'reminder', 'correspondence', 'opposition_reminder', 'opposition_correspondence'):
            continue
        docs = [doc for doc in documents if doc.get('sha256') == artifact['sha256']]
        if not docs:
            metadata = artifact.get('metadata') or {}
            docs = [{'source_key': source_key(artifact['kind'], metadata), 'kind': artifact['kind'], 'metadata': metadata,
                     'metadata_fingerprint': metadata_fingerprint(metadata), 'sha256': artifact['sha256'],
                     'filename': artifact['filename'], 'download_state': 'downloaded'}]
            documents.extend(docs)
        for doc in docs:
            try:
                fields, _logo = extract_local(content) if content.startswith(b'%PDF') else (None, None)
                key = doc['source_key']
                proceeding = ':'.join(key.split(':')[:2]) if not key.startswith('application:') else None
                events = document_events(content, doc['metadata'], proceeding)
                doc.update(fields=fields, events=events, parser_version=PARSER_VERSION,
                           parse_state='review_needed' if (fields or {}).get('_extraction', {}).get('needs_review') else 'parsed' if fields or events else 'review_needed')
                if fields and not any(item['sha256'] == artifact['sha256'] for item in extracted):
                    extracted.append({'sha256': artifact['sha256'], 'fields': fields, 'source_key': key})
                # This does not claim that old bytes have just been fetched.
                cache = store.cached_document(assignment['app_no'], key, metadata_fingerprint(doc['metadata']), 36500)
                store.cache_document(assignment['app_no'], key, metadata_fingerprint(doc['metadata']), artifact,
                                     paths[artifact['sha256']], fields, events, PARSER_VERSION)
                if cache:
                    with store.connect() as conn:
                        conn.execute('UPDATE document_cache SET checked_at=? WHERE app_no=? AND source_key=?',
                                     (cache['checked_at'], assignment['app_no'], key))
            except Exception:
                doc.update(parse_state='failed', fields=None, events=[])
                warnings.append('Document extraction needs retry: ' + doc['source_key'])
    stages = copy.deepcopy(original.get('stages') or {'core': {'state': 'complete', 'required': True}})
    complete = all(stage['state'] == 'complete' for stage in stages.values() if stage['required'])
    result = make_result(assignment, str(uuid.uuid4()), original['data'], artifacts, extracted, warnings,
                         'complete' if complete else 'partial')
    result.update(documents=documents, proceedings=original.get('proceedings', []), stages=stages,
                  repair_only=True, parser_version=PARSER_VERSION,
                  portal_observed_at=original.get('portal_observed_at') or original['observed_at'])
    return result, paths
