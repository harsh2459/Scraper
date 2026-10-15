"""Text extraction with bounded pages and explicit provenance/review outcomes."""
import io
import re
import pdfplumber
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PARSER_VERSION = '2'


def labeled_block(block, labels):
    """Support both value-before-label and label-before-value layouts.

    Infer direction from the first field boundary, not each blank field. Inline
    labels take precedence; longest labels prevent Address for Service from
    being mistaken for Address. Unmatched lines never become arbitrary keys.
    """
    labels = sorted(labels, key=len, reverse=True)
    tokens = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        matched = None
        for label in labels:
            if line.casefold() == label.casefold() or line.casefold() == (label + ':').casefold():
                matched = (label, '')
                break
            found = re.match(re.escape(label) + r'(?:\s*:\s*|\s+)(.+)$', line, re.I)
            if found:
                matched = (label, found.group(1).strip())
                break
        tokens.append(matched or (None, line))
    direction = 'before' if tokens and tokens[0][0] is None else 'after'
    values, pending, current = {}, [], None
    for label, text in tokens:
        if label is None:
            pending.append(text)
            continue
        if direction == 'before':
            values[label] = text or ' '.join(pending)
            pending = []
        else:
            if current is not None:
                values[current] = ' '.join(pending)
            current, pending = label, [text] if text else []
    if direction == 'after' and current is not None:
        values[current] = ' '.join(pending)
    return values


def pdf_text(content, with_info=False):
    chunks, used_ocr = [], False
    executable = os.environ.get('WORKER_OCR_EXECUTABLE') or shutil.which('tesseract')
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if len(pdf.pages) > 100:
            raise ValueError('PDF exceeds the 100-page extraction limit')
        for index, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            if len(text.strip()) < 20 and executable:
                if index >= 20:
                    raise ValueError('Scanned PDF exceeds the 20-page OCR limit')
                with tempfile.TemporaryDirectory() as directory:
                    image = Path(directory) / 'page.png'
                    page.to_image(resolution=200).original.save(image)
                    reply = subprocess.run([executable, str(image), 'stdout', '-l', os.environ.get('WORKER_OCR_LANGUAGE', 'eng'), '--psm', '6'],
                                           capture_output=True, timeout=60, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    text = reply.stdout.decode('utf-8', errors='replace')
                    used_ocr = True
            chunks.append(text)
    text = '\n'.join(chunks)
    return (text, used_ocr) if with_info else text


def application_fields(text):
    """Handle class/use blocks independently; a missing suffix is not fatal.

    Only matched values are emitted. None means an explicitly blank labeled
    value; absence means not matched. This prevents incomplete extraction from
    silently clearing a previously verified field.
    """
    if not re.search(r'FORM\s+TM\s*-\s*(A|1)\b|TRADE\s+MARKS\s+APPLICATION', text, re.I):
        return None
    result = {'form_type': 'TM-1' if re.search(r'FORM\s+TM\s*-\s*1', text, re.I) else 'TM-A'}
    def match(key, pattern):
        found = re.search(pattern, text, re.I | re.S)
        if found:
            result[key] = re.sub(r'\s+', ' ', found.group(1)).strip() or None
    match('temp_ref_no', r'Temp\.?\s*Ref\.?\s*No\s*:?\s*([^\s]+)')
    match('fee', r'\bFEE\s*:?\s*([\d,.]+)')
    match('application_filed_as', r'APPLICATION\s+FILED\s+AS\s*:?\s*([^\n]+)')
    if 'application_filed_as' not in result:
        match('application_filed_as', r'APPLICATION\s+FILED\s*\n([^\n]+)\n\s*AS\s*:')
    match('application_date', r'(?:Date\s+of\s+Application|Application\s+Date)\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})')
    match('receipt_no', r'Receipt\s+No\.?\s*:\s*(\S+)')
    match('receipt_date', r'Receipt\s+No[^\n]*\n\s*Date\s*:\s*(\S+)')
    match('receipt_amount', r'Amount\s*:\s*([\d,.]+)')
    match('language', r'\bLanguage\s*:?\s*([^\n]+)')
    # Match both "Class: 35 Description: ..." and description-before-class.
    block = re.search(r'CLASS\s+OF\s+GOODS\s+OR\s+SERVICE[S]?\s*(.*?)(?=STATEMENT\s+AS\s+TO|USE\s+OF\s+MARK|ANY\s+OTHER\s+IMPORTANT|VERIFICATION|$)', text, re.I | re.S)
    class_text = block.group(1) if block else text
    classes = []
    for found in re.finditer(r'\bClass\s*:\s*((?:\d{1,2}\s*[,/&]\s*)*\d{1,2})\b', class_text, re.I):
        numbers = re.findall(r'\d+', found.group(1))
        if not all(1 <= int(n) <= 45 for n in numbers):
            continue
        following = class_text[found.end():]
        following = re.split(r'\bClass\s*:', following, maxsplit=1, flags=re.I)[0]
        description = re.sub(r'\bDescription\s*:', '', following, flags=re.I)
        description = re.split(r'proposed\s+to\s+be\s+used|used\s+since', description, maxsplit=1, flags=re.I)[0]
        description = re.sub(r'\s+', ' ', description).strip()
        for number in numbers:
            classes.append({'number': number, 'description': description or None})
    if classes:
        if block:
            before = re.split(r'\bClass\s*:', class_text, maxsplit=1, flags=re.I)[0]
            before = re.sub(r'\s+', ' ', re.sub(r'Description\s*:', '', before, flags=re.I)).strip()
            if before:
                classes[0]['description'] = ' '.join(v for v in (before, classes[0]['description']) if v)
        result['classes'] = classes
        result['class'] = {'number': ', '.join(dict.fromkeys(c['number'] for c in classes)),
                           'description': '; '.join(c['description'] for c in classes if c['description']) or None}
    use = re.search(r'(?:STATEMENT\s+AS\s+TO\s+(?:THE\s+)?USE\s+OF\s+MARK|USE\s+OF\s+MARK)\s*:?\s*(.*?)(?=ANY\s+OTHER\s+IMPORTANT|VERIFICATION|I\s+hereby\s+verify|$)', text, re.I | re.S)
    if use and use.group(1).strip():
        result['use_statement'] = re.sub(r'\s+', ' ', use.group(1)).strip()
    else:
        match('use_statement', r'(proposed\s+to\s+be\s+used|used\s+since\s+\d{1,2}[/-]\d{1,2}[/-]\d{4})')
        if 'use_statement' not in result and use and ':' in use.group(0).split('\n', 1)[0]:
            result['use_statement'] = None
    match('other_information', r'ANY\s+OTHER\s+IMPORTANT\s+INFORMATION(?:\s+OR\s+STATEMENT)?\s*:?\s*(.*?)(?=VERIFICATION|I\s+hereby\s+verify|$)')
    # Colon/inline labels have an unambiguous direction. Bare labels from the
    # older reversed PDF layout remain handled by the compatibility parser.
    groups = [
        ('applicant', r'APPLICANT\s*\n\s*Applicant\s+No\.?\s*1\s*\n(.*?)(?=APPLICANT.S\s+AGENT|MARK\s+DETAILS)',
         {'Name': 'name', 'Address': 'address', 'Address for Service': 'address_for_service', 'Country': 'country', 'Mobile No.': 'mobile', 'Email Address': 'email', 'Legal Status': 'legal_status'}),
        ('agent', r'APPLICANT.S\s+AGENT[^\n]*\n(.*?)(?=MARK\s+DETAILS)',
         {'Name': 'name', 'Address': 'address', 'Nature of Agent': 'nature', 'Registration No.': 'registration_no'}),
        ('mark', r'MARK\s+DETAILS\s*\n(.*?)(?=IF\s+MARK|Trademark\s+Image|CLASS\s+OF|$)',
         {'Category of Mark': 'category', 'Trade Mark': 'trade_mark', 'Image Description': 'image_description'})]
    for group, pattern, labels in groups:
        found = re.search(pattern, text, re.I | re.S)
        if not found:
            continue
        values = {labels[label]: value or None for label, value in labeled_block(found.group(1), labels).items()}
        if values.get('mobile') and not re.fullmatch(r'\+?[\d*]{10,11}', values['mobile'].replace(' ', '')):
            del values['mobile']
        if values.get('email') and not re.fullmatch(r'[\w.*+-]+@[\w.*-]+', values['email']):
            del values['email']
        if values:
            result[group] = values
    return result


def merge_fields(base, supplement):
    merged = {key: merge_fields(value, {}) if isinstance(value, dict) else value
              for key, value in (base or {}).items() if value is not None}
    for key, value in (supplement or {}).items():
        if isinstance(value, dict):
            merged[key] = {**(merged.get(key) or {}), **value}
        else:
            merged[key] = value
    return merged or None
