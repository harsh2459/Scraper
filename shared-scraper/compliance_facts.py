"""Source facts, not legal advice or title-based deadline guesses."""
import calendar
from datetime import date, datetime, timedelta
import re
from bs4 import BeautifulSoup


def parse_date(value):
    if isinstance(value, date):
        return value
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def add_period(anchor, value, unit):
    if unit == 'days':
        return anchor + timedelta(days=value)
    if unit == 'months':
        month = anchor.month - 1 + value
        year, month = anchor.year + month // 12, month % 12 + 1
        return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
    raise ValueError('Unknown period unit')


def document_text(content):
    if content.startswith(b'%PDF'):
        from pdf_scraper.text_fields import pdf_text
        return pdf_text(content)
    soup = BeautifulSoup(content, 'html.parser')
    for node in soup(['script', 'style', 'input']):
        node.decompose()
    return soup.get_text(' ', strip=True)


def document_events(content, metadata, proceeding=None):
    ocr_used = False
    if content.startswith(b'%PDF'):
        from pdf_scraper.text_fields import pdf_text
        text, ocr_used = pdf_text(content, with_info=True)
    else:
        text = document_text(content)
    events = events_from_text(text, metadata, proceeding)
    for event in events:
        event['facts']['ocr_used'] = ocr_used
        if ocr_used:
            event['needs_review'] = True
    return events


def events_from_text(text, metadata, proceeding=None):
    text = re.sub(r'\s+', ' ', text).strip()
    subject = str(metadata.get('subject') or metadata.get('description') or '')
    combined = subject + ' ' + text
    issue = parse_date(metadata.get('corres_date') or metadata.get('date'))
    issued = re.search(r'(?:issued\s+on|date\s+of\s+issue|dated)\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.I)
    if issued:
        issue = parse_date(issued.group(1)) or issue
    event_type, category, side = None, None, 'own_mark'
    # Body takes precedence over misleading titles such as "Hearing Notice".
    if re.search(r'(?:application|mark).{0,90}(?:has been|is hereby|is)\s+accepted|accepted\s+for\s+(?:advertisement|registration)', text, re.I):
        event_type, category = 'application_accepted', 'application_accepted'
    elif re.search(r'notice\s+(?:of\s+)?opposition|counter[ -]?statement', combined, re.I):
        event_type, category = 'counterstatement_due', 'counter_statement'
    elif re.search(r'\bu/s\s*132\b', combined, re.I):
        event_type, category = 'examination_reply_due', 'reply_us_132'
    elif re.search(r'examination\s+report', combined, re.I):
        event_type, category = 'examination_reply_due', 'examination_report_reply'
    elif re.search(r'hearing|show\s+cause', combined, re.I):
        event_type, category = 'hearing_scheduled', 'hearing_opposition' if proceeding else 'hearing_show_cause'
    elif re.search(r'evidence.{0,60}rule\s*45', combined, re.I):
        event_type, category, side = 'evidence_due', 'evidence_rule_45', 'opposition_out'
    elif re.search(r'evidence.{0,60}rule\s*46', combined, re.I):
        event_type, category = 'evidence_due', 'evidence_rule_46'
    elif re.search(r'evidence.{0,60}rule\s*47', combined, re.I):
        event_type, category, side = 'evidence_due', 'evidence_rule_47', 'opposition_out'
    if not event_type:
        return []
    if event_type == 'hearing_scheduled' and proceeding:
        side = 'both'
    event_date, due, basis, anchor, value, unit = None, None, 'unknown', None, None, None
    if event_type == 'hearing_scheduled':
        found = re.search(r'(?:hearing\s+(?:is\s+)?(?:scheduled|fixed|appointed)\s+(?:for|on)|date\s+of\s+hearing|hearing\s+date)\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.I)
        if found:
            event_date = due = parse_date(found.group(1))
            basis = 'stated'
    elif category not in ('application_accepted',):
        found = re.search(r'(?:on\s+or\s+before|due\s+date\s*:|last\s+date\s*:)\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.I)
        if found:
            due, basis = parse_date(found.group(1)), 'stated'
        period = re.search(r'within\s+(?:(?:a|the)\s+period\s+of\s+)?(\d+|one|two|three)\s+(calendar\s+)?(months?|days?)\s+(?:from|of|after)\s+(?:the\s+)?(?:date\s+of\s+)?(receipt|service|issue|issuance)', text, re.I)
        if period:
            words = {'one': 1, 'two': 2, 'three': 3}
            value = words.get(period.group(1).lower()) or int(period.group(1))
            unit = 'months' if period.group(3).lower().startswith('month') else 'days'
            anchor = 'receipt_date' if period.group(4).lower() in ('receipt', 'service') else 'issue_date'
            if not due and anchor == 'issue_date' and issue:
                due, basis = add_period(issue, value, unit), 'document_period'
    return [{'event_type': event_type, 'category': category, 'applies_to_side': side,
             'issue_date': issue.isoformat() if issue else None, 'event_date': event_date.isoformat() if event_date else None,
             'due_date': due.isoformat() if due else None, 'due_basis': basis,
             'period_value': value, 'period_unit': unit, 'anchor': anchor,
             'needs_review': basis != 'stated' or event_type == 'application_accepted',
             'facts': {'subject': subject, 'excerpt': text[:4000], 'parser_version': '2'}}]
