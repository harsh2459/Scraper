"""
Standalone IP-block diagnostic — replicates the EXACT session flow
ipindia_scraper.py's fetch_captcha() uses (GET /OTP/index to init session
cookies, THEN GET /Otp/GetCaptcha), instead of hitting GetCaptcha cold.

Why this matters: ipindia_scraper.py's own get_captcha_for_display()
docstring documents that a bare/out-of-sequence GetCaptcha call
intermittently 401s even under normal conditions ("typically when the
preceding session-init GET to /OTP/index didn't fully take"). A one-off
script that skips the /OTP/index warm-up isn't a fair comparison against
the real scraper's request pattern — this script closes that gap so a
local-vs-server comparison is apples-to-apples.

Usage (identical on both the server and a local machine):
    python scripts/diagnose_ip_block.py
    python scripts/diagnose_ip_block.py --runs 5          # repeat, show consistency
    python scripts/diagnose_ip_block.py --proxy http://user:pass@host:port

Run this on Contabo AND on a local machine, compare the two outputs.
"""

import argparse
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

BASE = "https://tmrsearch.ipindia.gov.in/estatus"
CAPTCHA_URL = f"{BASE}/Otp/GetCaptcha"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _snippet(resp: requests.Response, n: int = 300) -> str:
    try:
        return resp.text[:n].replace("\n", " ").strip()
    except Exception:
        return "<unreadable body>"


def one_run(run_no: int, proxy: str | None) -> dict:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://tmrsearch.ipindia.gov.in",
        "Referer": f"{BASE}/OTP/index",
    })
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}

    print(f"\n--- run {run_no} ---")

    # Step 1: session-init GET (this is what the real scraper does first —
    # skipping this is what makes a naive test unfair)
    t0 = time.monotonic()
    try:
        r1 = sess.get(f"{BASE}/OTP/index", timeout=15)
        dt1 = time.monotonic() - t0
        print(f"  GET /OTP/index          -> {r1.status_code}  ({dt1:.2f}s)  cookies={list(sess.cookies.keys())}")
    except Exception as exc:
        print(f"  GET /OTP/index          -> EXCEPTION: {exc}")
        return {"run": run_no, "index_ok": False, "captcha_status": None, "error": str(exc)}

    # Step 2: the actual CAPTCHA fetch, same session/cookies
    t0 = time.monotonic()
    try:
        r2 = sess.get(CAPTCHA_URL, timeout=15)
        dt2 = time.monotonic() - t0
        print(f"  GET /Otp/GetCaptcha     -> {r2.status_code}  ({dt2:.2f}s)")
        print(f"    body: {_snippet(r2)!r}")
        return {
            "run": run_no, "index_ok": True, "captcha_status": r2.status_code,
            "error": None,
        }
    except Exception as exc:
        print(f"  GET /Otp/GetCaptcha     -> EXCEPTION: {exc}")
        return {"run": run_no, "index_ok": True, "captcha_status": None, "error": str(exc)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=3, help="how many independent session attempts (default 3)")
    ap.add_argument("--proxy", default=None, help="optional proxy URL to route through (http://user:pass@host:port)")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between runs (default 3)")
    args = ap.parse_args()

    print(f"Diagnosing IP India reachability — {args.runs} run(s), "
          f"{'via proxy ' + args.proxy if args.proxy else 'direct connection'}")

    results = []
    for i in range(1, args.runs + 1):
        results.append(one_run(i, args.proxy))
        if i < args.runs:
            time.sleep(args.delay)

    print("\n=== SUMMARY ===")
    ok = sum(1 for r in results if r["captcha_status"] == 200)
    print(f"{ok}/{len(results)} GetCaptcha calls returned 200")
    for r in results:
        status = r["captcha_status"] if r["captcha_status"] is not None else f"ERROR: {r['error']}"
        print(f"  run {r['run']}: {status}")

    if ok == 0:
        print("\nEvery run failed — consistent with either an IP block or a")
        print("portal-side outage. Compare this output against the SAME script")
        print("run on a different network to tell those apart.")
    elif ok < len(results):
        print("\nInconsistent (some 200, some not) — leans toward intermittent")
        print("portal flakiness rather than a hard IP block, which would be")
        print("consistently rejected on every attempt.")
    else:
        print("\nAll runs succeeded — no block from this network right now.")


if __name__ == "__main__":
    main()
