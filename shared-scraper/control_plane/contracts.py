"""Version 1 wire contracts, shared by coordinator and offline workers."""
import datetime as dt
import hashlib
import json
import re
import uuid

UTC = dt.timezone.utc


def utcnow():
    return dt.datetime.now(UTC)


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid observation timestamp.') from exc
    if parsed.tzinfo is None:
        raise ValueError('Timestamps must include a timezone.')
    return parsed.astimezone(UTC)


def application_number(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,12}', value):
        raise ValueError('Application number must contain 1-12 digits.')
    return value


def identifier(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Expected a UUID identifier.') from exc


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_result(body):
    if not isinstance(body, dict) or body.get('schema_version') != 1:
        raise ValueError('Unsupported result schema; expected version 1.')
    for field in ('result_id', 'job_id', 'reservation_token'):
        identifier(body.get(field))
    application_number(body.get('app_no'))
    observed = timestamp(body.get('observed_at'))
    if observed > utcnow() + dt.timedelta(minutes=5):
        raise ValueError('Observation timestamp is in the future.')
    if type(body.get('generation')) is not int or body['generation'] < 1:
        raise ValueError('Invalid generation.')
    if not isinstance(body.get('data'), dict) or not body['data']:
        raise ValueError('A nonempty data object is required.')
    artifacts = body.get('artifacts')
    if not isinstance(artifacts, list) or len(artifacts) > 2000:
        raise ValueError('Invalid artifact manifest.')
    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError('Invalid artifact.')
        sha = artifact.get('sha256', '')
        if not re.fullmatch('[a-f0-9]{64}', sha) or sha in seen:
            raise ValueError('Invalid or repeated artifact checksum.')
        seen.add(sha)
        name = artifact.get('filename', '')
        if not isinstance(name, str) or not name or len(name) > 180 or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('Unsafe artifact filename.')
        if type(artifact.get('size')) is not int or artifact['size'] < 1:
            raise ValueError('Artifact size must be positive.')
        if artifact.get('kind') not in ('document', 'logo', 'reminder', 'correspondence', 'pr_details', 'app_details',
                                        'proceeding', 'opposition_reminder', 'opposition_correspondence'):
            raise ValueError('Unknown artifact kind.')
    if body.get('completeness') not in ('complete', 'partial'):
        raise ValueError('Invalid completeness state.')
    if not isinstance(body.get('extracted'), list) or not isinstance(body.get('warnings'), list):
        raise ValueError('Invalid extraction/warnings lists.')
    if any(not isinstance(w, str) or len(w) > 1000 for w in body['warnings']):
        raise ValueError('Warnings must be strings of at most 1000 characters.')
    for extraction in body.get('extracted', []):
        if not isinstance(extraction, dict) or extraction.get('sha256') not in seen or not isinstance(extraction.get('fields'), dict):
            raise ValueError('Extracted fields must reference a declared artifact.')
    for artifact in artifacts:
        if 'metadata' in artifact and not isinstance(artifact['metadata'], dict):
            raise ValueError('Artifact metadata must be an object.')
    data = body['data']
    stages = body.get('stages')
    if stages is not None:
        if not isinstance(stages, dict) or not stages or len(stages) > 2200:
            raise ValueError('Invalid stage outcomes.')
        for name, stage in stages.items():
            if not isinstance(stage, dict) or stage.get('state') not in ('pending', 'complete', 'failed') or type(stage.get('required')) is not bool:
                raise ValueError('Invalid stage outcome.')
        if body['completeness'] == 'complete' and any(s['state'] != 'complete' for s in stages.values() if s['required']):
            raise ValueError('Required stages must be complete.')
    for name in ('documents', 'proceedings'):
        if name in body and (not isinstance(body[name], list) or len(body[name]) > 2000):
            raise ValueError('Invalid ' + name + ' manifest.')
    keys = set()
    for doc in body.get('documents', []):
        if (not isinstance(doc, dict) or not isinstance(doc.get('source_key'), str)
                or not 1 <= len(doc['source_key']) <= 512 or doc['source_key'] in keys
                or not isinstance(doc.get('metadata'), dict)
                or doc.get('download_state') not in ('downloaded', 'failed')
                or doc.get('parse_state') not in ('pending', 'parsed', 'failed', 'review_needed')
                or doc.get('kind') not in ('document', 'reminder', 'correspondence', 'opposition_reminder', 'opposition_correspondence')
                or not isinstance(doc.get('events', []), list) or len(doc.get('events', [])) > 20):
            raise ValueError('Invalid document outcome.')
        keys.add(doc['source_key'])
        if doc['download_state'] == 'downloaded' and doc.get('sha256') not in seen:
            raise ValueError('Downloaded documents must reference an artifact.')
        if doc['download_state'] == 'failed' and body['completeness'] == 'complete':
            raise ValueError('Complete refreshes cannot include failed downloads.')
        if doc.get('fields') is not None and not isinstance(doc['fields'], dict):
            raise ValueError('Invalid document fields.')
        for event in doc.get('events', []):
            if (not isinstance(event, dict) or event.get('event_type') not in
                    ('application_accepted', 'counterstatement_due', 'examination_reply_due', 'hearing_scheduled', 'evidence_due')
                    or not isinstance(event.get('category'), str) or len(event['category']) > 80
                    or event.get('due_basis') not in ('unknown', 'stated', 'document_period')
                    or event.get('applies_to_side') not in ('own_mark', 'opposition_out', 'both')
                    or not isinstance(event.get('facts'), dict) or type(event.get('needs_review')) is not bool):
                raise ValueError('Invalid document event.')
            from compliance_facts import parse_date
            for field in ('issue_date', 'event_date', 'due_date'):
                if event.get(field) is not None and not parse_date(event[field]):
                    raise ValueError('Invalid event date.')
            if event.get('period_value') is not None and (type(event['period_value']) is not int or not 1 <= event['period_value'] <= 36525):
                raise ValueError('Invalid notice period.')
            if event.get('period_unit') not in (None, 'months', 'days') or event.get('anchor') not in (None, 'receipt_date', 'issue_date'):
                raise ValueError('Invalid notice period anchor/unit.')
    for proceeding in body.get('proceedings', []):
        if not isinstance(proceeding, dict) or not proceeding.get('proceeding_key') or not proceeding.get('opposition_no'):
            raise ValueError('Invalid proceeding identity.')
    if body['completeness'] == 'complete' and ((body.get('warnings') and stages is None) or not data.get('Class')
            or not (data.get('Application Date') or data.get('Date of Application'))):
        raise ValueError('Complete results require class/date fields and no missing-panel warnings.')
    return observed


def fingerprint(body):
    return digest(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
