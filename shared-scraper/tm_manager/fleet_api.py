"""
Flask Blueprint for the fleet coordinator's worker-facing API — every
/api/fleet/* route a local worker machine polls (Contabo-coordinator +
local-worker-fleet migration, see the plan doc).

Auth: NOT handled in this module — tm_manager/app.py's single global
@app.before_request gate already covers every route on this Flask app,
including these, via the existing OPS_API_USER/OPS_API_PASS Basic Auth
credential (same one used for curl-based ops/monitoring calls elsewhere —
see app.py's _check_ops_credentials). A worker machine authenticates the
exact same way; nothing new was added or needs to be added here.

Kept as its own blueprint (rather than adding routes to app.py directly),
same rationale as tm_leads_routes.py: this is a distinct trust boundary
(machine-to-service polling, never a browser session) and a distinct
feature area from the rest of tm_manager's routes.
"""

import logging

from flask import Blueprint, jsonify, request

import config
import db

bp = Blueprint("fleet", __name__, url_prefix="/api/fleet")


@bp.route("/register", methods=["POST"])
def register():
    body = request.json or {}
    machine_key = (body.get("machine_key") or "").strip()
    display_name = (body.get("display_name") or "").strip() or None
    if not machine_key:
        return jsonify({"error": "machine_key is required."}), 400
    conn = db.get_connection()
    try:
        machine = db.upsert_fleet_machine(conn, machine_key, display_name)
    finally:
        conn.close()
    return jsonify({"machine_id": machine["id"], "machine_key": machine["machine_key"]})


@bp.route("/heartbeat", methods=["POST"])
def heartbeat():
    body = request.json or {}
    machine_key = (body.get("machine_key") or "").strip()
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else None
    if not machine_key:
        return jsonify({"error": "machine_key is required."}), 400
    conn = db.get_connection()
    try:
        ok = db.record_fleet_heartbeat(conn, machine_key, meta)
    finally:
        conn.close()
    if not ok:
        # Not registered (yet, or ever) — tell the caller to register
        # rather than silently doing nothing; a worker's heartbeat loop
        # treats this as "go call /register again".
        return jsonify({"error": "Unknown machine_key — register first.", "registered": False}), 404
    return jsonify({"success": True})


@bp.route("/assignment", methods=["GET"])
def assignment():
    """A worker's "give me my batch" poll. Atomically flips this machine's
    own 'assigned' fleet_assignments rows (for today) to 'claimed' and
    returns them alongside the machine's currently-assigned otp_email_pool
    accounts — everything the worker's local scheduler needs to run a
    fleet-scoped batch (see claim_fleet_assigned_batch on the worker side,
    which then does its OWN atomic claim against these exact rows before
    actually fetching anything; this endpoint marking them 'claimed' is
    assignment-tracking for the dashboard, not the fetch-safety claim
    itself — that still happens locally, same FOR UPDATE SKIP LOCKED
    pattern as every other claim in this codebase)."""
    machine_key = (request.args.get("machine_key") or "").strip()
    limit = request.args.get("limit", 15, type=int)
    if not machine_key:
        return jsonify({"error": "machine_key is required."}), 400
    conn = db.get_connection()
    try:
        machine = db.get_fleet_machine(conn, machine_key)
        if machine is None:
            return jsonify({"error": "Unknown machine_key — register first.", "registered": False}), 404
        if machine["status"] != "active":
            return jsonify({"machine_id": machine["id"], "accounts": [], "trademarks": [], "disabled": True})

        rows = db.claim_fleet_assignment_rows_for_worker(conn, machine["id"], limit)
        accounts = conn.execute(
            "SELECT id, email_address, app_password, imap_server, imap_port "
            "FROM otp_email_pool WHERE assigned_machine_id = %(mid)s AND enabled",
            {"mid": machine["id"]},
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "machine_id": machine["id"],
        "accounts": accounts,
        "trademarks": [
            {"assignment_id": r["assignment_id"], "my_trademark_id": r["my_trademark_id"], "app_no": r["app_no"]}
            for r in rows
        ],
    })


@bp.route("/report", methods=["POST"])
def report():
    """Fired by a worker after its own local mark_trademark_fetched/
    mark_trademark_error call already succeeded — purely for dashboard
    visibility (fleet_assignments.status), never load-bearing for fetch
    correctness. A failure here is logged, never surfaced as an error to
    the worker, since the real state is already safely written."""
    body = request.json or {}
    assignment_id = body.get("assignment_id")
    status = (body.get("status") or "").strip()
    error = body.get("error")
    if not isinstance(assignment_id, int) or status not in ("done", "failed"):
        return jsonify({"error": "assignment_id (int) and status ('done'|'failed') are required."}), 400
    conn = db.get_connection()
    try:
        ok = db.report_fleet_assignment_result(conn, assignment_id, status)
    finally:
        conn.close()
    if not ok:
        logging.warning("Fleet: report for unknown assignment_id=%s (status=%s, error=%s)", assignment_id, status, error)
    return jsonify({"success": True})
