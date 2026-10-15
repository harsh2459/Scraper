"""Pure parsers for saved eRegister formats; no sessions or configuration."""
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

PORTAL_BASE = 'https://tmrsearch.ipindia.gov.in/estatus'


def require_portal_fragment(html):
    """An empty/error/login response is not a successful empty list."""
    if not isinstance(html, str) or not html.strip():
        raise ValueError('Empty portal response')
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select('input[type=password], #OtpCode, #CaptchaAnswer, input[name="CaptchModel.CaptchaAnswer"]'):
        raise ValueError('Portal authentication response instead of data')
    text = soup.get_text(' ', strip=True).lower()
    if re.search(r'session\s+(?:has\s+)?expired|access\s+denied|internal\s+server\s+error|please\s+(?:log\s*in|login)|invalid\s+token', text):
        raise ValueError('Portal returned an error response')
    return soup


def require_list_fragment(html, uploaded=False):
    soup = require_portal_fragment(html)
    if uploaded:
        recognized = bool(soup.select('table.document-table'))
    else:
        recognized = any(any(len(_cells(row)) >= 6 and _label(_cells(row)[0]) in ('sno', 'srno')
                             and _label(_cells(row)[1]) in ('corresno', 'correspno') for row in _rows(table))
                         for table in soup.find_all('table'))
    if not recognized and not re.search(r'no\s+(?:records?|data|documents?|reminders?|correspondence|notices?)(?:\s+(?:were|are|is))?\s+(?:found|available)|no\s+records?\s+to\s+display', soup.get_text(' ', strip=True), re.I):
        raise ValueError('Unrecognized portal list response; cannot prove an empty list')
    return soup


def portal_url(reference):
    """Resolve portal/root-relative links without duplicating /estatus."""
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError('A portal document link is required.')
    reference = reference.strip()
    if reference.startswith('estatus/'):
        reference = '/' + reference
    url = urljoin(PORTAL_BASE + '/', reference)
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'tmrsearch.ipindia.gov.in'
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Document links must stay on the HTTPS portal.')
    return url


def _rows(table):
    return [tr for tr in table.find_all('tr') if tr.find_parent('table') is table]


def _cells(row):
    return row.find_all(['td', 'th'], recursive=False)


def _label(cell):
    return re.sub(r'[^a-z0-9]', '', cell.get_text(' ', strip=True).lower())


def parse_notice_table(html):
    """Seven-column notice tables; headers may be td, in nested layouts."""
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    for table in soup.find_all('table'):
        rows = _rows(table)
        if not any(len(_cells(row)) >= 6 and _label(_cells(row)[0]) in ('sno', 'srno')
                   and _label(_cells(row)[1]) in ('corresno', 'correspno') for row in rows):
            continue
        for row in rows:
            cells = _cells(row)
            if len(cells) < 6 or not cells[0].get_text(strip=True).isdigit():
                continue
            item = dict(zip(('s_no', 'corres_no', 'corres_date', 'subject', 'despatch_no', 'despatch_date'),
                            (c.get_text(' ', strip=True) for c in cells[:6])))
            link = cells[6].find('a') if len(cells) >= 7 else None
            onclick = link.get('onclick', '') if link else ''
            match = re.search(r"ViewReminderDocument\(\s*['\"]([^'\"]+)['\"]\s*\)", onclick)
            href = link.get('href') if link else None
            item.update(corres_num=match.group(1) if match else None,
                        view_url=href if href and not href.startswith(('#', 'javascript:')) else None)
            result.append(item)
    return result


def parse_opposition_rows(html):
    """Discover records from live links; opaque tokens are never constructed."""
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    for row in soup.find_all('tr'):
        cells = _cells(row)
        if len(cells) != 6:
            continue
        links = [a.get('href', '') for cell in cells[4:6] for a in cell.find_all('a', href=True)]
        detail = next((u for u in links if urlparse(u).path.endswith('/ShowOppRecDetails')), None)
        if not detail:
            continue
        reminder = next((u for u in links if urlparse(u).path.endswith('/Get_Opposition_Reminder_Correspound')), None)
        query = parse_qs(urlparse(detail).query)
        result.append({'filed_date': cells[0].get_text(' ', strip=True),
                       'opposition_no': cells[1].get_text(' ', strip=True),
                       'opponent_code': cells[2].get_text(' ', strip=True),
                       'opponent_name_address': cells[3].get_text('\n', strip=True),
                       'portal_type': query.get('typ', [None])[0],
                       'details_url': detail, 'reminders_url': reminder})
    return result


def parse_opposition_record(html):
    fields = {'opprecno': 'opposition_reference', 'opprecdate': 'filed_date',
              'opponentcode': 'opponent_code', 'opponentname': 'opponent_name',
              'opponentaddress': 'opponent_address', 'agentattorneyname': 'opponent_agent_name',
              'agentattorneyaddress': 'opponent_agent_address', 'status': 'proceeding_status', 'decision': 'decision'}
    soup = BeautifulSoup(html, 'html.parser')
    result = {}
    for table in soup.find_all('table'):
        for row in _rows(table):
            cells = _cells(row)
            if len(cells) == 2 and _label(cells[0]) in fields:
                result[fields[_label(cells[0])]] = cells[1].get_text(' ', strip=True) or None
    result['correspondence'] = parse_notice_table(html)
    return result


def application_document_links(html):
    """Standalone examination reports can be absent from upload lists."""
    soup = BeautifulSoup(html, 'html.parser')
    seen, result = set(), []
    for link in soup.find_all('a', href=True):
        href = link['href']
        if urlparse(href).path.lower().endswith('/examreport') and href not in seen:
            seen.add(href)
            result.append({'document_id': 'examination-report', 'subject': 'Examination Report', 'view_url': href})
    return result
