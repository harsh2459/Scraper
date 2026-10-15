"""
Pure-Python grouping of a single date's IP India search rows into 'ranges'
(numeric application-number clusters) and passthrough entries (name/other
non-numeric searches), for the search-history browsing UI.
"""

import re

GAP_THRESHOLD = 25

# Applicant/proprietor company-type classification, detected from the name
# text itself (the extracted "Legal Status" field is free-text prose, not a
# clean tag, so pattern-matching the name is more reliable across all
# results). Order matters: checked top to bottom, first match wins.
APPLICANT_TYPES = [
    "LLP",
    "Private Limited",
    "Limited",
    "Partnership",
    "Proprietorship",
    "HUF",
    "Trust",
    "Society",
    "Company (Other)",
    "Individual / Other",
]

_APPLICANT_TYPE_PATTERNS = [
    ("LLP", re.compile(r"\bLLP\b")),
    ("Private Limited", re.compile(r"\bPRIVATE\s+LIMITED\b|\bPVT\.?\s*LTD\.?\b")),
    ("Limited", re.compile(r"\bLIMITED\b|\bLTD\.?\b")),
    ("Partnership", re.compile(r"\bPARTNER(SHIP)?\b")),
    ("Proprietorship", re.compile(r"\bPROPRIETOR(SHIP)?\b|\bTRADING AS\b|\bM/S\.?\b")),
    ("HUF", re.compile(r"\bHUF\b")),
    ("Trust", re.compile(r"\bTRUST\b")),
    ("Society", re.compile(r"\bSOCIETY\b")),
    ("Company (Other)", re.compile(r"\bCOMPANY\b|\bCO\.\b|\bCORPORATION\b|\bCORP\.?\b|\bINC\.?\b")),
]


def classify_applicant_type(name: str | None) -> str | None:
    """
    Best-effort company-type classification from an applicant/proprietor
    name string (e.g. "ABC PRIVATE LIMITED" -> "Private Limited",
    "JOHN DOE PROPRIETOR OF XYZ" -> "Proprietorship"). Returns
    "Individual / Other" for a plain personal/trade name with no legal-form
    marker, or None if name is blank.
    """
    if not name or not name.strip():
        return None
    upper = name.upper()
    for label, pattern in _APPLICANT_TYPE_PATTERNS:
        if pattern.search(upper):
            return label
    return "Individual / Other"


CONTACT_VISIBILITY_LABELS = ["Unmasked (shown in full)", "Masked by portal"]


def classify_contact_visibility(mobile: str | None, email: str | None) -> str | None:
    """
    Whether an extracted applicant's mobile/email came through the portal
    already masked (e.g. "98******23", "g****@gmail.com") or fully
    unmasked/real (e.g. "9871112090", "legalcell@patanjaliayurved.org").
    IP India masks contact details inconsistently depending on the
    application — this flags which is which so real personal data is never
    shown without the user knowing it's unmasked.

    Returns "Unmasked (shown in full)" if EITHER field is present and not
    masked, "Masked by portal" if both present fields are masked, or None
    if there's no mobile/email at all to judge.
    """
    fields = [f for f in (mobile, email) if f]
    if not fields:
        return None
    if any("*" not in f for f in fields):
        return "Unmasked (shown in full)"
    return "Masked by portal"


def group_searches_for_date(rows: list[dict]) -> list[dict]:
    """
    rows: list of dict-like rows from db.list_ipindia_searches_for_date(),
          each with at least 'id', 'query', 'search_type', 'result_count',
          'error'.

    Returns a list of group dicts, each either:
        {"kind": "range", "start": int, "end": int, "count": int,
         "search_ids": [...], "label": "7465684-7465690"}
        (or label == str(start) when count == 1, no dash)
        {"kind": "single", "id": ..., "query": ..., "search_type": ...,
         "result_count": ..., "error": ...}

    Ordered: numeric ranges first (ascending by start), then non-numeric
    entries (alphabetically by query). Application numbers whose gap to the
    previous number (once sorted) is <= GAP_THRESHOLD join the same range;
    a bigger gap starts a new range.
    """
    numeric: list[tuple[int, dict]] = []
    non_numeric: list[dict] = []
    for row in rows:
        q = (row.get("query") or "").strip()
        if q.isdigit():
            numeric.append((int(q), row))
        else:
            non_numeric.append(row)

    numeric.sort(key=lambda pair: pair[0])

    ranges: list[dict] = []
    current: list[tuple[int, dict]] = []
    for num, row in numeric:
        if current and (num - current[-1][0]) > GAP_THRESHOLD:
            ranges.append(_finalize_range(current))
            current = []
        current.append((num, row))
    if current:
        ranges.append(_finalize_range(current))

    non_numeric.sort(key=lambda r: (r.get("query") or "").lower())
    passthrough = [
        {
            "kind": "single",
            "id": r["id"],
            "query": r["query"],
            "search_type": r["search_type"],
            "result_count": r.get("result_count"),
            "error": r.get("error"),
        }
        for r in non_numeric
    ]

    return ranges + passthrough


def _finalize_range(items: list[tuple[int, dict]]) -> dict:
    nums = [n for n, _ in items]
    start, end = nums[0], nums[-1]
    return {
        "kind": "range",
        "start": start,
        "end": end,
        "count": len(items),
        "search_ids": [r["id"] for _, r in items],
        "label": str(start) if start == end else f"{start}-{end}",
    }
