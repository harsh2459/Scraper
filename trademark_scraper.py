"""
Local scraper for quickcompany.in trademark listings.

Given a list of search queries (brand names), fetches EVERY matching
trademark for each query via the site's paginated /trademarks search, then
scrapes each match's full profile (application ID, status, classes,
proprietor, agent, dates, description).

Every result is saved to the local SQLite database (quickcompany.db) AND
written to its own folder under data/<query>/ - one JSON file per matched
trademark plus a combined results.csv.

Usage:
    python trademark_scraper.py "Tata"
    python trademark_scraper.py --input brands.txt

Browse everything collected so far with the web UI:
    python app.py
"""

import argparse
import sys
import time

import requests

import db
from folder_export import write_trademark_search_folder
from trademark_scraper_core import DEFAULT_DELAY_SECONDS, make_session, scrape_all_matches


def load_queries(args: argparse.Namespace) -> list[str]:
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return args.queries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("queries", nargs="*", help="Trademark/brand names to look up")
    parser.add_argument("--input", help="Path to a text file with one brand name per line")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Seconds to wait between requests")
    args = parser.parse_args()

    queries = load_queries(args)
    if not queries:
        parser.error("Provide brand names as arguments or via --input")

    session = make_session()
    conn = db.get_connection()
    results = []

    for i, query in enumerate(queries, start=1):
        print(f"[{i}/{len(queries)}] Searching: {query}", file=sys.stderr)

        def report(done, total, name):
            print(f"    [{done}/{total}] Scraping: {name}", file=sys.stderr)

        try:
            query_results = scrape_all_matches(session, query, delay=args.delay, on_progress=report)
        except requests.RequestException as exc:
            query_results = [{"query": query, "error": f"request failed: {exc}"}]

        for result in query_results:
            db.save_trademark_result(conn, result)
        results.extend(query_results)

        folder = write_trademark_search_folder(query, query_results)
        print(f"    Wrote {len(query_results)} file(s) to {folder}", file=sys.stderr)

        if i < len(queries):
            time.sleep(args.delay)

    conn.close()
    print(f"Saved {len(results)} result(s) to quickcompany.db", file=sys.stderr)


if __name__ == "__main__":
    main()
