"""
Writes scraped results for one trademark search query into its own folder
under data/, e.g. searching "glomix" creates data/glomix/ containing:

    data/glomix/glomix-chemicals-india-private-limited.json
    ...
    data/glomix/results.csv          <- every match, one row each
    data/glomix/all_results.json     <- every match, full detail combined
"""

import csv
import json
import re
from pathlib import Path

from trademark_scraper_core import FIELD_LABELS as TRADEMARK_FIELD_LABELS

DATA_DIR = Path(__file__).parent / "data"

TRADEMARK_CSV_FIELDNAMES = ["query", "name", "profile_url", *TRADEMARK_FIELD_LABELS, "error"]


def slugify(text: str) -> str:
    slug = text.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "search"


def _item_filename(result: dict) -> str:
    base = result.get("name") or result.get("query") or "unknown"
    return f"{slugify(base)}.json"


def write_trademark_search_folder(query: str, results: list[dict]) -> Path:
    """Create data/<slug-of-query>/ for a trademark search."""
    folder = DATA_DIR / slugify(query)
    folder.mkdir(parents=True, exist_ok=True)

    used_names: dict[str, int] = {}
    for result in results:
        filename = _item_filename(result)
        if filename in used_names:
            used_names[filename] += 1
            stem = filename[:-5]
            filename = f"{stem}-{used_names[filename]}.json"
        else:
            used_names[filename] = 0

        with open(folder / filename, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    with open(folder / "results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADEMARK_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            writer.writerow(result)

    with open(folder / "all_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    return folder
