"""
Core scraping logic for quickcompany.in trademark listings.

Given a search query (brand name), fetches every matching result from the
site's paginated /trademarks search, then visits each matching trademark
page and extracts the public trademark information block (application ID,
status, classes, proprietor, agent, dates, description).

If the site requires email OTP verification, the scraper will automatically
read the OTP from your configured inbox (see config.py / Settings page).
"""

import re
import time
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config
from email_otp import get_otp

BASE_URL = "https://www.quickcompany.in"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DEFAULT_DELAY_SECONDS = 3.0
TRADEMARK_SEARCH_URL = f"{BASE_URL}/trademarks"
MAX_PAGES = 50

# Max times to retry an OTP challenge on a single page
_MAX_OTP_RETRIES = 2


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


FIELD_LABELS = [
    "Application ID",
    "Status",
    "Sub Status",
    "Date of Application",
    "Classes",
    "Type",
    "State",
    "Country",
    "Filing Mode",
    "IP Office",
    "Used Since",
    "Valid / Upto",
    "Description",
    "PR Details",
]


class ScrapeError(Exception):
    pass


# ── OTP page detection & submission ──────────────────────────────────────────

# Keywords that strongly suggest an OTP challenge page
_OTP_INDICATORS = [
    "enter otp",
    "enter the otp",
    "otp sent",
    "verify your email",
    "verification code",
    "one time password",
    "please verify",
    "enter code sent",
    "enter the code",
    "resend otp",
]


def _is_otp_page(soup: BeautifulSoup) -> bool:
    """Return True if the page is showing an OTP / email-verification form."""
    text = soup.get_text(" ", strip=True).lower()
    return any(ind in text for ind in _OTP_INDICATORS)


def _otp_form_details(
    soup: BeautifulSoup, page_url: str
) -> tuple[str, str, dict]:
    """
    Extract (form_action_url, otp_field_name, hidden_fields) from an OTP page.

    Falls back gracefully when the form cannot be fully parsed.
    """
    form = soup.find("form")
    if not form:
        return page_url, "otp", {}

    # Resolve form action
    action = form.get("action", "").strip()
    if action and not action.startswith("http"):
        action = urllib.parse.urljoin(BASE_URL, action)
    if not action:
        action = page_url

    # Identify which input is the OTP field
    otp_field_name = "otp"
    for inp in form.find_all("input"):
        inp_type = (inp.get("type") or "text").lower()
        if inp_type in ("hidden", "submit", "button", "checkbox", "radio"):
            continue
        name = (inp.get("name") or "").lower()
        id_ = (inp.get("id") or "").lower()
        placeholder = (inp.get("placeholder") or "").lower()
        for keyword in ("otp", "code", "token", "pin", "verification"):
            if keyword in name or keyword in id_ or keyword in placeholder:
                otp_field_name = inp.get("name") or "otp"
                break

    # Collect hidden fields (CSRF tokens, etc.)
    hidden: dict[str, str] = {}
    for inp in form.find_all("input", type="hidden"):
        n = inp.get("name", "")
        if n:
            hidden[n] = inp.get("value", "")

    return action, otp_field_name, hidden


def _handle_otp_challenge(
    session: requests.Session,
    otp_page_url: str,
    soup: BeautifulSoup,
) -> Optional[requests.Response]:
    """
    When an OTP page is detected:
      1. Read the OTP from the configured email inbox.
      2. Submit it to the site's form.
      3. Return the response after OTP submission, or None on failure.
    """
    if not config.is_otp_configured():
        raise ScrapeError(
            "Site requested email OTP verification but no email credentials are "
            "configured. Go to Settings and enter your email + app password."
        )

    form_action, otp_field, hidden_fields = _otp_form_details(soup, otp_page_url)

    # Read OTP from inbox
    otp = get_otp(
        email_address=config.OTP_EMAIL_ADDRESS,
        app_password=config.OTP_APP_PASSWORD,
        imap_server=config.OTP_IMAP_SERVER,
        imap_port=config.OTP_IMAP_PORT,
        timeout=config.OTP_TIMEOUT_SECONDS,
        poll_interval=config.OTP_POLL_INTERVAL,
        sender_filter=config.OTP_SENDER_FILTER or None,
        subject_filter=config.OTP_SUBJECT_FILTER or None,
    )
    if not otp:
        raise ScrapeError(
            f"OTP not received within {config.OTP_TIMEOUT_SECONDS}s. "
            "Check that IMAP is enabled and the email address is correct."
        )

    # Submit OTP
    payload = dict(hidden_fields)
    payload[otp_field] = otp
    resp = session.post(form_action, data=payload, timeout=20)
    resp.raise_for_status()
    return resp


# ── Search results helpers ────────────────────────────────────────────────────


def _total_matches(soup: BeautifulSoup) -> int | None:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"of\s+([\d,]+)\s+Matches", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def _page_profile_links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    seen = set()
    results = []
    for link in soup.select('a[href^="/trademarks/"]'):
        href = link["href"]
        if href in seen or "/trademarks/journals" in href:
            continue
        seen.add(href)
        name = link.get_text(strip=True)
        results.append((name, urllib.parse.urljoin(BASE_URL, href)))
    return results


def find_all_matches(
    session: requests.Session, query: str
) -> list[tuple[str, str]]:
    """
    Search quickcompany.in trademarks for *query*, returning every (name, url)
    pair across all pages.  Handles OTP challenges automatically.
    """
    all_results: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    page = 1
    total = None
    otp_retries = 0

    while page <= MAX_PAGES:
        params = {"q": query}
        if page > 1:
            params["page"] = page

        resp = session.get(TRADEMARK_SEARCH_URL, params=params, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Handle OTP challenge
        if _is_otp_page(soup):
            if otp_retries >= _MAX_OTP_RETRIES:
                raise ScrapeError("OTP challenge could not be resolved after retries.")
            otp_retries += 1
            resp = _handle_otp_challenge(session, resp.url, soup)
            soup = BeautifulSoup(resp.text, "html.parser")
        else:
            otp_retries = 0

        if total is None:
            total = _total_matches(soup)

        page_results = _page_profile_links(soup)
        new_results = [r for r in page_results if r[1] not in seen_urls]
        if not new_results:
            break

        for r in new_results:
            seen_urls.add(r[1])
        all_results.extend(new_results)

        if total is not None and len(all_results) >= total:
            break
        page += 1

    return all_results


def parse_profile(html: str, profile_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    data: dict[str, str] = {"profile_url": profile_url}

    name_el = soup.select_one("nav h1")
    data["name"] = name_el.get_text(strip=True) if name_el else ""

    proprietor_el = soup.select_one("nav h4")
    data["proprietor_summary"] = (
        proprietor_el.get_text(strip=True) if proprietor_el else ""
    )

    info_block = soup.select_one("#information")
    container = info_block.parent if info_block is not None else soup

    labels = container.find_all("div", class_="pb-0")
    for label_div in labels:
        label = label_div.get_text(strip=True).rstrip(":")
        if label not in FIELD_LABELS:
            continue
        value_div = label_div.find_next_sibling("div")
        if value_div is None:
            continue
        input_el = value_div.select_one("input")
        if input_el is not None:
            value = input_el.get("value", "").strip()
        else:
            value = value_div.get_text(" ", strip=True)
        data[label] = value

    proprietor_blocks = container.find_all("div", class_="pb-0")
    for label_div in proprietor_blocks:
        if label_div.get_text(strip=True).rstrip(":") == "Proprietor":
            values = label_div.find_next_siblings("div", limit=2)
            texts = [v.get_text(" ", strip=True) for v in values if v.get_text(strip=True)]
            data["Proprietor"] = " | ".join(texts)
        if label_div.get_text(strip=True).rstrip(":") == "Agent":
            values = label_div.find_next_siblings("div", limit=2)
            texts = [v.get_text(" ", strip=True) for v in values if v.get_text(strip=True)]
            data["Agent"] = " | ".join(texts)

    return data


def scrape_profile(
    session: requests.Session,
    profile_url: str,
    query: str = "",
    max_retries: int = 3,
) -> dict:
    """Fetch one trademark profile page, handling OTP challenges automatically."""
    otp_retries = 0

    for attempt in range(max_retries + 1):
        try:
            resp = session.get(profile_url, timeout=20)
        except requests.RequestException as exc:
            return {"query": query, "profile_url": profile_url, "error": f"request failed: {exc}"}

        if resp.status_code == 429:
            if attempt < max_retries:
                time.sleep(10 * (attempt + 1))
                continue
            return {"query": query, "profile_url": profile_url, "error": "rate limited (429) after retries"}

        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            return {"query": query, "profile_url": profile_url, "error": f"request failed: {exc}"}

        soup = BeautifulSoup(resp.text, "html.parser")

        # Handle OTP challenge on detail pages
        if _is_otp_page(soup):
            if otp_retries >= _MAX_OTP_RETRIES:
                return {"query": query, "profile_url": profile_url, "error": "OTP challenge could not be resolved"}
            otp_retries += 1
            try:
                resp = _handle_otp_challenge(session, resp.url, soup)
                soup = BeautifulSoup(resp.text, "html.parser")
            except ScrapeError as exc:
                return {"query": query, "profile_url": profile_url, "error": str(exc)}

        try:
            data = parse_profile(resp.text, profile_url)
        except ScrapeError as exc:
            return {"query": query, "profile_url": profile_url, "error": str(exc)}
        data["query"] = query
        return data

    return {"query": query, "profile_url": profile_url, "error": "max retries exceeded"}


def scrape_all_matches(
    session: requests.Session,
    query: str,
    delay: float = DEFAULT_DELAY_SECONDS,
    on_progress=None,
) -> list[dict]:
    matches = find_all_matches(session, query)
    if not matches:
        return [{"query": query, "error": "no match found"}]

    results = []
    for i, (name, profile_url) in enumerate(matches, start=1):
        if on_progress:
            on_progress(i, len(matches), name)
        results.append(scrape_profile(session, profile_url, query))
        if i < len(matches):
            time.sleep(delay)
    return results
