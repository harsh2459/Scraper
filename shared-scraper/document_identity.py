"""Document identity excludes encrypted session locators.

Uploaded documents without a verified ID use description/date/duplicate ordinal.
This is a fallback, not proof that the portal has not replaced the bytes; periodic
full fetches recheck them. Notice references are scoped to their proceeding.
"""
import json
from control_plane.contracts import digest

PARSER_VERSION = '2'


def meaningful(metadata):
    return {k: v for k, v in metadata.items() if k not in
            ('portal_locator', 'view_url', 'corres_num', 'details_url', 'reminders_url', 'source_key')}


def metadata_fingerprint(metadata):
    return digest(json.dumps(meaningful(metadata), sort_keys=True, ensure_ascii=False).encode())


def source_key(kind, metadata, ordinal=1, proceeding='application'):
    reference = metadata.get('corres_no') or metadata.get('document_id')
    if reference:
        return f'{proceeding}:{kind}:{reference}'
    signature = {k: metadata.get(k) for k in ('description', 'date', 'subject', 'corres_date')}
    return f'{proceeding}:{kind}:fallback:{metadata_fingerprint(signature)}:{ordinal}'
