"""
Parses an official IP India Trade Marks Journal PDF into journal_entries,
then scores every portfolio trademark against every parsed entry and stores
matches above a similarity threshold into journal_matches.

Callable two ways:

  - By hand: python scripts/parse_journal.py "Journal/2274.pdf"
  - From the running app: run_journal_job(pdf_path, job_id) — updates the
    given journal_jobs row's progress as it works, so app.py's upload
    endpoint can run this in a background thread and the frontend can poll
    /api/journal/jobs/<job_id> for status.

Each journal page is one trademark entry with a fixed field order (header
line with journal no/date/class, optional word-mark line, "<app_no>
<app_date>" anchor line, applicant name, applicant address, ..., goods
description, page-number footer) — see the docstring on _parse_page for the
exact layout this relies on. That same layout is also what makes the logo
extraction possible: the mark's artwork always sits in the page region
between the header line and the app-no line, so app.py's
journal_entry_logo route crops that region directly out of the PDF page on
demand (using APP_NO_DATE_RE, exported below, to relocate that line's
position without re-running the full entry parse).
"""

import difflib
import logging
import re
import sys
import time
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

_HEADER_RE = re.compile(
    r"Trade Marks Journal No:\s*(?P<journal_no>\S+)\s*,\s*(?P<journal_date>\S+)"
    r"(?:\s+Class\s+(?P<class_number>\d+))?",
)
APP_NO_DATE_RE = re.compile(r"^(?P<app_no>\d{5,10})\s+(?P<app_date>\d{2}/\d{2}/\d{4})$")
# A trailing lone integer line is the printed page number, not content.
_PAGE_FOOTER_RE = re.compile(r"^\d{1,5}$")

# Similarity below this is noise (nearly every two short strings score
# something under difflib); only pairs at or above it are worth a human's
# attention, so only these get stored.
_MIN_SIMILARITY = 0.55
# How many top matches to keep per portfolio trademark — a mark that's
# genuinely similar to hundreds of journal entries doesn't happen in
# practice, and capping keeps journal_matches from growing unbounded.
_MAX_MATCHES_PER_TRADEMARK = 20

# How often (in pages) to write progress back to journal_jobs — every page
# would be needless DB traffic over a ~1800-page run.
_PROGRESS_EVERY_PAGES = 20


def _parse_page(text: str) -> dict | None:
    """Extract one journal entry's fields from a single page's text, or
    None if this page doesn't look like a trademark entry (e.g. the
    journal's own front-matter/notice pages)."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None

    header_match = _HEADER_RE.search(lines[0])
    if not header_match or not header_match.group("class_number"):
        return None

    # Drop the trailing page-number footer line, if present, so it never
    # gets mistaken for content.
    if lines and _PAGE_FOOTER_RE.match(lines[-1]):
        lines = lines[:-1]

    body = lines[1:]
    app_no_idx = None
    for i, ln in enumerate(body):
        if APP_NO_DATE_RE.match(ln):
            app_no_idx = i
            break
    if app_no_idx is None:
        return None

    app_match = APP_NO_DATE_RE.match(body[app_no_idx])
    # A device-only mark (logo, no text) has no line before the app-no
    # anchor; a word mark has exactly one.
    word_mark = body[app_no_idx - 1] if app_no_idx > 0 else None
    applicant_name = body[app_no_idx + 1] if len(body) > app_no_idx + 1 else None
    applicant_address = body[app_no_idx + 2] if len(body) > app_no_idx + 2 else None

    return {
        "journal_no": header_match.group("journal_no"),
        "journal_date": header_match.group("journal_date"),
        "class_number": header_match.group("class_number"),
        "app_no": app_match.group("app_no"),
        "app_date": app_match.group("app_date"),
        "word_mark": word_mark,
        "applicant_name": applicant_name,
        "applicant_address": applicant_address,
    }


def parse_journal_pdf(pdf_path: str, on_progress=None) -> list[dict]:
    """on_progress(pages_done, total_pages, entries_found), called every
    _PROGRESS_EVERY_PAGES pages if given. Each entry's page_number is only
    meaningful together with source_file (this PDF's own filename) — a
    journal split across several class-range files restarts page numbering
    at 1 in each one, so journal_entry_logo (app.py) needs both to find the
    right page in the right file."""
    entries = []
    source_file = Path(pdf_path).name
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            entry = _parse_page(text)
            if entry:
                entry["page_number"] = i + 1
                entry["source_file"] = source_file
                entries.append(entry)
            if on_progress and (i + 1) % _PROGRESS_EVERY_PAGES == 0:
                on_progress(i + 1, total, len(entries))
        if on_progress:
            on_progress(total, total, len(entries))
    return entries


def store_entries(conn, entries: list[dict]) -> None:
    for e in entries:
        conn.execute(
            """
            INSERT INTO journal_entries
                (journal_no, journal_date, app_no, app_date, class_number,
                 word_mark, applicant_name, applicant_address, page_number, source_file)
            VALUES
                (%(journal_no)s, %(journal_date)s, %(app_no)s, %(app_date)s, %(class_number)s,
                 %(word_mark)s, %(applicant_name)s, %(applicant_address)s, %(page_number)s, %(source_file)s)
            ON CONFLICT (journal_no, app_no) DO UPDATE SET
                journal_date = EXCLUDED.journal_date,
                app_date = EXCLUDED.app_date,
                class_number = EXCLUDED.class_number,
                word_mark = EXCLUDED.word_mark,
                applicant_name = EXCLUDED.applicant_name,
                applicant_address = EXCLUDED.applicant_address,
                page_number = EXCLUDED.page_number,
                source_file = EXCLUDED.source_file
            """,
            e,
        )
    conn.commit()


def _normalize(mark: str | None) -> str:
    if not mark:
        return ""
    return re.sub(r"[^A-Z0-9]+", " ", mark.upper()).strip()


def compute_matches(conn, journal_no: str, on_progress=None) -> int:
    """Score every portfolio trademark's word mark against every entry in
    this journal (device-only marks with no word_mark are skipped on both
    sides — there's no text to compare), keeping the top
    _MAX_MATCHES_PER_TRADEMARK matches at/above _MIN_SIMILARITY per
    trademark. Returns how many match rows were written.

    Two things make this safe to interrupt (a deploy restarting the
    service mid-run used to silently discard EVERY match for EVERY
    journal, every time — see the 2026-09-21 incident where journal_jobs
    showed 'done' with matches_found=0/NULL across the board because the
    single conn.commit() at the very end never got a chance to run):

    1. Commits after each portfolio trademark's own matches are written,
       not once at the very end — if the process is killed mid-run (e.g.
       systemctl restart during a deploy), every trademark already
       scored keeps its matches; only the trademarks not yet reached are
       left for the next run to redo, not the whole journal.
    2. A length-ratio pre-filter before ever calling
       difflib.SequenceMatcher: SequenceMatcher.ratio() can mathematically
       never exceed 2*min(len_a, len_b) / (len_a + len_b) (its own quick
       upper bound), so once normalized lengths differ enough that this
       upper bound is already below _MIN_SIMILARITY, the real (expensive)
       comparison is skipped entirely — this is what makes the ~1,929 x
       2,300+ comparisons per journal finish in seconds instead of
       minutes, which is what let a normal service restart interrupt it
       in the first place.

    on_progress(done, total), called after each trademark if given — the
    per-trademark commit above already means "how far did we get" is a
    real, meaningful number, so this can drive a job's progress column.
    """
    portfolio = conn.execute(
        """
        SELECT t.id, COALESCE(r.data_json -> 0 ->> 'Trade Mark', t.label) AS word_mark
        FROM my_trademarks t
        LEFT JOIN ipindia_results r ON r.id = t.last_search_id
        """
    ).fetchall()
    portfolio = [(row["id"], _normalize(row["word_mark"])) for row in portfolio if row["word_mark"]]

    journal_rows = conn.execute(
        "SELECT id, word_mark FROM journal_entries WHERE journal_no = %(journal_no)s AND word_mark IS NOT NULL",
        {"journal_no": journal_no},
    ).fetchall()
    journal_rows = [(row["id"], _normalize(row["word_mark"])) for row in journal_rows if row["word_mark"]]
    journal_rows = [(entry_id, mark, len(mark)) for entry_id, mark in journal_rows if mark]

    written = 0
    total = len(portfolio)
    for done, (tm_id, tm_mark) in enumerate(portfolio, start=1):
        if not tm_mark:
            if on_progress:
                on_progress(done, total)
            continue
        tm_len = len(tm_mark)
        # Same argument order as the original SequenceMatcher(None, tm_mark,
        # entry_mark) call — tm_mark is seq "a" (set_seq1, held fixed across
        # this trademark's whole inner loop), entry_mark is seq "b"
        # (set_seq2, varies per entry). difflib's autojunk heuristic is
        # computed from seq "b", so swapping which string plays which role
        # changes the ratio slightly — getting this order backwards was
        # caught by a before/after comparison against the old brute-force
        # algorithm on real data (959 vs 961 matches) before this shipped.
        matcher = difflib.SequenceMatcher()
        matcher.set_seq1(tm_mark)
        scored = []
        for entry_id, entry_mark, entry_len in journal_rows:
            # Cheap length-only upper bound BEFORE the real (expensive)
            # comparison — mathematically valid, never produces a false
            # negative (see docstring above).
            upper_bound = 2 * min(tm_len, entry_len) / (tm_len + entry_len)
            if upper_bound < _MIN_SIMILARITY:
                continue
            matcher.set_seq2(entry_mark)
            ratio = matcher.ratio()
            if ratio >= _MIN_SIMILARITY:
                scored.append((entry_id, ratio))
        scored.sort(key=lambda x: x[1], reverse=True)
        for entry_id, ratio in scored[:_MAX_MATCHES_PER_TRADEMARK]:
            conn.execute(
                """
                INSERT INTO journal_matches (my_trademark_id, journal_entry_id, similarity)
                VALUES (%(tm_id)s, %(entry_id)s, %(sim)s)
                ON CONFLICT (my_trademark_id, journal_entry_id) DO UPDATE SET similarity = EXCLUDED.similarity
                """,
                {"tm_id": tm_id, "entry_id": entry_id, "sim": ratio},
            )
            written += 1
        # Commit after EACH trademark, not once at the end (see docstring)
        # — this one line is what makes a mid-run kill lose only the
        # not-yet-reached trademarks instead of the entire journal.
        conn.commit()
        if on_progress:
            on_progress(done, total)
    return written


def run_journal_job(pdf_path: str, job_id: int | None = None) -> dict:
    """Parses pdf_path end to end (entries + matches), updating the given
    journal_jobs row's progress/status as it goes if job_id is given.
    Returns a small summary dict either way. Raises on failure after
    recording the error on the job row, so a background-thread caller's
    exception still ends up visible to whoever's polling the job."""
    conn = db.get_connection() if job_id is not None else None
    try:
        if job_id is not None:
            db.update_journal_job(conn, job_id, status="parsing")

        def _progress(done, total, found):
            if job_id is not None:
                db.update_journal_job(conn, job_id, total_pages=total, pages_done=done, entries_found=found)

        entries = parse_journal_pdf(pdf_path, on_progress=_progress)
        if not entries:
            if job_id is not None:
                db.update_journal_job(conn, job_id, status="error", error="No trademark entries found in this PDF.")
            return {"entries": 0, "matches": 0, "journal_no": None}

        journal_no = entries[0]["journal_no"]
        if job_id is not None:
            db.update_journal_job(conn, job_id, journal_no=journal_no, status="storing")

        store_conn = conn if conn is not None else db.get_connection()
        try:
            store_entries(store_conn, entries)
            if job_id is not None:
                db.update_journal_job(conn, job_id, status="matching")

            def _match_progress(done, total):
                if job_id is not None:
                    db.update_journal_job(conn, job_id, pages_done=done, total_pages=total)

            n_matches = compute_matches(store_conn, journal_no, on_progress=_match_progress)
        finally:
            if store_conn is not conn:
                store_conn.close()

        if job_id is not None:
            db.update_journal_job(conn, job_id, status="done", matches_found=n_matches)
        return {"entries": len(entries), "matches": n_matches, "journal_no": journal_no}
    except Exception as exc:
        if job_id is not None and conn is not None:
            db.update_journal_job(conn, job_id, status="error", error=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()


def run_journal_batch_job(
    pdf_paths: list[str],
    job_id: int,
    journal_no_override: str | None = None,
    journal_date_override: str | None = None,
) -> dict:
    """Same end result as run_journal_job, but for several PDFs at once —
    IP India splits one week's journal across multiple class-range files
    (Class 1-9, 10-25, ...) that all share one journal number/date. Parses
    and stores each file in turn (each file's own entries land under
    whatever journal_no/journal_date it declares, or the override if one
    was given at upload time — see app.py's journal_upload route), then
    runs the portfolio similarity match ONCE at the end across everything
    just stored, since re-matching after every single file would repeat
    the same ~200-portfolio-marks x N-journal-entries comparison for no
    benefit until every file's entries are in.
    """
    conn = db.get_connection()
    total_entries = 0
    resolved_journal_no = journal_no_override
    try:
        db.update_journal_job(conn, job_id, status="parsing", total_files=len(pdf_paths))
        for i, pdf_path in enumerate(pdf_paths):
            filename = Path(pdf_path).name
            db.update_journal_job(conn, job_id, current_filename=filename, files_done=i, pages_done=0, total_pages=None)

            def _progress(done, total, found, _i=i):
                db.update_journal_job(
                    conn, job_id,
                    total_pages=total, pages_done=done,
                    entries_found=total_entries + found,
                )

            entries = parse_journal_pdf(pdf_path, on_progress=_progress)
            if not entries:
                continue

            if journal_no_override:
                for e in entries:
                    e["journal_no"] = journal_no_override
                    if journal_date_override:
                        e["journal_date"] = journal_date_override
            elif resolved_journal_no is None:
                resolved_journal_no = entries[0]["journal_no"]

            store_entries(conn, entries)
            total_entries += len(entries)
            db.update_journal_job(conn, job_id, journal_no=resolved_journal_no, entries_found=total_entries)

        db.update_journal_job(conn, job_id, files_done=len(pdf_paths), status="matching")

        if resolved_journal_no is None:
            db.update_journal_job(conn, job_id, status="error", error="No trademark entries found in any uploaded file.")
            return {"entries": 0, "matches": 0, "journal_no": None}

        def _match_progress(done, total):
            db.update_journal_job(conn, job_id, pages_done=done, total_pages=total)

        n_matches = compute_matches(conn, resolved_journal_no, on_progress=_match_progress)
        db.update_journal_job(conn, job_id, status="done", matches_found=n_matches)
        return {"entries": total_entries, "matches": n_matches, "journal_no": resolved_journal_no}
    except Exception as exc:
        db.update_journal_job(conn, job_id, status="error", error=str(exc))
        raise
    finally:
        conn.close()


def recompute_all_matches(conn=None) -> dict:
    """Self-healing recovery pass — re-runs compute_matches for EVERY
    journal that already has parsed entries on file, regardless of what
    journal_jobs.status says. Call this once at app startup (see
    app.py's __main__ block).

    Why this exists: before 2026-09-21, compute_matches held one big
    uncommitted transaction and committed once at the very end. A code
    deploy's `systemctl restart tmtracker-backend` — something that
    happens on essentially every push — could (and reliably did) kill the
    background matching thread mid-run, silently discarding every match
    for that journal even though journal_jobs already showed 'done' (set
    right after the parse/store step, before matching had a chance to
    finish). compute_matches now commits after each portfolio trademark
    and is fast enough (length-filtered before the expensive comparison)
    that simply redoing it in full, for every journal, on every startup,
    is cheap and fully idempotent (ON CONFLICT DO UPDATE) — so no journal
    can ever again be left with incomplete matches after a deploy, and
    nobody needs to SSH in and re-run anything by hand.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.get_connection()
    try:
        journals = db.list_journal_numbers(conn)
        results = {}
        for j in journals:
            journal_no = j["journal_no"]
            try:
                n = compute_matches(conn, journal_no)
                results[journal_no] = n
            except Exception:
                logging.exception("recompute_all_matches: journal %s failed, skipping", journal_no)
        return results
    finally:
        if own_conn:
            conn.close()


def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <path-to-journal.pdf>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    t0 = time.time()
    print(f"Parsing {pdf_path} ...")

    def _print_progress(done, total, found):
        print(f"  parsed {done}/{total} pages, {found} entries so far")

    entries = parse_journal_pdf(pdf_path, on_progress=_print_progress)
    print(f"Parsed {len(entries)} entries in {time.time() - t0:.1f}s")
    if not entries:
        print("No entries found — nothing to store.")
        return

    journal_no = entries[0]["journal_no"]
    conn = db.get_connection()
    try:
        print(f"Storing entries for journal {journal_no} ...")
        store_entries(conn, entries)
        print("Computing similarity matches against the portfolio ...")
        t1 = time.time()
        n = compute_matches(conn, journal_no)
        print(f"Stored {n} matches in {time.time() - t1:.1f}s")
    finally:
        conn.close()

    print(f"Done in {time.time() - t0:.1f}s total.")


if __name__ == "__main__":
    main()
