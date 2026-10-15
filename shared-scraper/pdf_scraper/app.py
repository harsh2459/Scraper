"""
PDF Scraper (Scraper B) — a standalone service whose only job is to save
PDF bytes handed to it by the main IP India scraper (Scraper A) and, for
the main application form only, extract structured fields from it.

Never talks to IP India, never claims an email-pool account, never logs in
to anything. Localhost-only by design (see the bind address at the bottom
of this file) — the only caller is Scraper A, on the same machine.
"""

import base64 as _b64
import logging
import os

import sentry_sdk
from flask import Flask, jsonify, request
from sentry_sdk.integrations.flask import FlaskIntegration

import pdf_config as config  # noqa: F401 — must import before pdf_processor (see pdf_config.py)
from pdf_processor import process_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Sentry must be initialized before the Flask app is created. No-op (empty
# dsn) if SENTRY_DSN_PDF isn't set — safe for local dev.
sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN_PDF", ""),
    integrations=[FlaskIntegration()],
    traces_sample_rate=0.1,
    send_default_pii=False,
)

app = Flask(__name__)


if config.BASIC_AUTH_USER and config.BASIC_AUTH_PASS:
    @app.before_request
    def _require_basic_auth():
        hdr = request.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                u, _, p = _b64.b64decode(hdr[6:]).decode("utf-8", "replace").partition(":")
                if u == config.BASIC_AUTH_USER and p == config.BASIC_AUTH_PASS:
                    return None
            except Exception:
                pass
        return ("Auth required", 401, {"WWW-Authenticate": 'Basic realm="pdf-scraper"'})


@app.route("/api/process-pdf", methods=["POST"])
def api_process_pdf():
    """multipart form: file (the PDF/HTML bytes), app_no, description,
    optional search_id, optional kind ("document" default | "reminder" |
    "correspondence"). Returns {"saved_path": ..., "extracted": {...}|null}."""
    uploaded = request.files.get("file")
    app_no = (request.form.get("app_no") or "").strip()
    description = (request.form.get("description") or "").strip()
    kind = (request.form.get("kind") or "document").strip()
    ext = (request.form.get("ext") or "pdf").strip()
    search_id_raw = request.form.get("search_id")
    search_id = int(search_id_raw) if search_id_raw and search_id_raw.isdigit() else None

    if uploaded is None or not app_no or not description:
        return jsonify({"error": "file, app_no, and description are required."}), 400

    file_bytes = uploaded.read()
    if not file_bytes:
        return jsonify({"error": "Uploaded file was empty."}), 400

    try:
        result = process_pdf(file_bytes, app_no, description, kind=kind, search_id=search_id, ext=ext)
    except Exception as exc:
        logging.exception("process_pdf failed for %s / %s", app_no, description)
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Bound to 127.0.0.1 only — never reachable from outside this machine,
    # let alone the internet. Scraper A is the only caller, over localhost.
    if config.USE_WAITRESS:
        from waitress import serve
        logging.info("Serving pdf_scraper with waitress on 127.0.0.1:%d", config.PORT)
        serve(app, host="127.0.0.1", port=config.PORT, threads=4)
    else:
        app.run(host="127.0.0.1", port=config.PORT, debug=False, threaded=True)
