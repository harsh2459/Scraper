"""
Flask Blueprint for the TM Lead & Invoice system — every /api/tm/* route.

Kept as its own blueprint (rather than adding to app.py directly) since this
is a distinct feature area from the existing portfolio-tracking routes;
app.py just imports and registers it.
"""

import json

from flask import Blueprint, g, jsonify, request
import config

import tm_leads_db as tdb

bp = Blueprint("tm_leads", __name__, url_prefix="/api/tm")


@bp.before_request
def protect_shared_leads():
    # This legacy shared CRM has no per-user ownership. Restrict it until
    # a tenant-aware model is implemented; UI routing is not authorization.
    if config.FLEET_CONTROL_ENABLED and not getattr(g, 'is_admin', False):
        return jsonify(error='Administrator access required for shared CRM data.'), 403


def _json():
    return request.get_json(silent=True) or {}


# ─── Leads CRUD ────────────────────────────────────────────────────────────

@bp.route("/leads", methods=["GET"])
def leads_list():
    conn = tdb.get_connection()
    try:
        filters = {
            "status": request.args.get("status", "").strip(),
            "consultant_id": request.args.get("consultant_id", "").strip(),
            "class": request.args.get("class", "").strip(),
            "assigned_to": request.args.get("assigned_to", "").strip(),
            "payment_status": request.args.get("payment_status", "").strip(),
            "date_from": request.args.get("date_from", "").strip(),
            "date_to": request.args.get("date_to", "").strip(),
            "q": request.args.get("q", "").strip(),
        }
        leads = tdb.list_leads(conn, filters)
    finally:
        conn.close()
    return jsonify({"leads": leads, "total": len(leads)})


@bp.route("/leads", methods=["POST"])
def leads_create():
    conn = tdb.get_connection()
    try:
        lead = tdb.create_lead(conn, _json())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()
    return jsonify(lead), 201


@bp.route("/leads/kanban", methods=["GET"])
def leads_kanban():
    conn = tdb.get_connection()
    try:
        grouped = tdb.kanban_grouped(conn)
    finally:
        conn.close()
    return jsonify({"stages": tdb.KANBAN_STAGES, "grouped": grouped})


@bp.route("/leads/invoices", methods=["GET"])
def leads_invoices():
    conn = tdb.get_connection()
    try:
        filters = {"payment_status": request.args.get("payment_status", "").strip(), "q": request.args.get("q", "").strip()}
        leads = tdb.list_leads(conn, filters)
    finally:
        conn.close()
    return jsonify({"leads": leads, "total": len(leads)})


@bp.route("/leads/global-search", methods=["GET"])
def leads_global_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"leads": []})
    conn = tdb.get_connection()
    try:
        leads = tdb.list_leads(conn, {"q": q})
    finally:
        conn.close()
    return jsonify({"leads": leads[:20]})


@bp.route("/leads/<int:lead_id>", methods=["GET"])
def lead_detail(lead_id):
    conn = tdb.get_connection()
    try:
        lead = tdb.get_lead(conn, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        activities = tdb.list_activities(conn, lead_id)
    finally:
        conn.close()
    return jsonify({"lead": lead, "activities": activities})


@bp.route("/leads/<int:lead_id>", methods=["PUT"])
def lead_update(lead_id):
    conn = tdb.get_connection()
    try:
        lead = tdb.update_lead(conn, lead_id, _json())
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Status Update", "Lead info updated")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>", methods=["DELETE"])
def lead_delete(lead_id):
    conn = tdb.get_connection()
    try:
        tdb.delete_lead(conn, lead_id)
    finally:
        conn.close()
    return jsonify({"ok": True})


# ─── Lead sub-actions ──────────────────────────────────────────────────────

@bp.route("/leads/<int:lead_id>/qualify", methods=["POST"])
def lead_qualify(lead_id):
    data = _json()
    conn = tdb.get_connection()
    try:
        lead = tdb.set_lead_fields(conn, lead_id, data, {"qualification_status", "qualification_notes"})
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Status Update", f"Qualification set to {data.get('qualification_status', lead.get('qualification_status'))}")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>/map-consultant", methods=["POST"])
def lead_map_consultant(lead_id):
    data = _json()
    conn = tdb.get_connection()
    try:
        lead = tdb.set_lead_fields(conn, lead_id, data, {"consultant_id"})
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Workflow", "Consultant mapped")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>/update-payment", methods=["POST"])
def lead_update_payment(lead_id):
    data = _json()
    conn = tdb.get_connection()
    try:
        lead = tdb.set_lead_fields(conn, lead_id, data, {"pricing", "package", "payment_status"})
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Accounting", f"Payment status: {lead.get('payment_status')}")
    finally:
        conn.close()
    return jsonify(lead)


# Every document-workflow step name mapped to its <name>_at timestamp column
# and <name>_path file-path column — one dict instead of thirteen near-
# identical route functions, since update-workflow's only per-call
# difference is which pair of columns it writes.
_WORKFLOW_STEPS = {
    "partnership_deed": ("partnership_deed_at", "partnership_deed_path"),
    "firm_pan": ("firm_pan_at", "firm_pan_path"),
    "partner_kyc": ("partner_kyc_at", "partner_kyc_path"),
    "udyam_reg": ("udyam_reg_at", "udyam_reg_path"),
    "brand_logo": ("brand_logo_at", "brand_logo_path"),
    "firm_email_verified": ("firm_email_verified_at", "firm_email_path"),
    "poa_drafted": ("poa_drafted_at", "poa_drafted_path"),
    "stamp_purchased": ("stamp_purchased_at", "stamp_purchased_path"),
    "poa_signed": ("poa_signed_at", "poa_signed_path"),
    "tma_drafted": ("tma_drafted_at", "tma_drafted_path"),
    "tma_shared": ("tma_shared_at", "tma_shared_path"),
    "invoice_generated": ("invoice_generated_at", "invoice_generated_path"),
    "final_payment": ("final_payment_at", "final_payment_path"),
}


@bp.route("/leads/<int:lead_id>/update-workflow", methods=["POST"])
def lead_update_workflow(lead_id):
    """Marks one document-workflow step done/undone. Accepts JSON
    {step, done, file_path} — file upload itself is handled by a separate
    generic upload endpoint that returns a storage path, kept decoupled so
    this route stays plain JSON like every other sub-action."""
    data = _json()
    step = data.get("step")
    if step not in _WORKFLOW_STEPS:
        return jsonify({"error": f"Unknown step: {step}"}), 400
    at_col, path_col = _WORKFLOW_STEPS[step]
    done = data.get("done", True)

    conn = tdb.get_connection()
    try:
        # "now()" needs to be a raw SQL expression, not a bound param — the
        # rest of set_lead_fields always binds values, so this one column is
        # built as its own tiny UPDATE instead of forcing that helper to
        # special-case a SQL literal.
        if done:
            conn.execute(f"UPDATE tm_leads SET {at_col} = now(), updated_at = now() WHERE id = %s", (lead_id,))
        else:
            conn.execute(f"UPDATE tm_leads SET {at_col} = NULL, updated_at = now() WHERE id = %s", (lead_id,))
        if "file_path" in data:
            conn.execute(f"UPDATE tm_leads SET {path_col} = %s WHERE id = %s", (data["file_path"], lead_id))
        conn.commit()

        lead = tdb.get_lead(conn, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Workflow", f"{step.replace('_', ' ').title()} marked {'done' if done else 'pending'}")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>/update-tracking", methods=["POST"])
def lead_update_tracking(lead_id):
    data = _json()
    allowed = {
        "arn_number", "application_no", "mark_status", "mark_status_date",
        "action_by_us", "status_comment", "reply_status", "status",
        "vienna_codification_at", "formalities_check_at",
        "arn_receipt_path", "fees_receipt_path", "e_main_page_path", "draft_path",
    }
    set_data = {k: v for k, v in data.items() if k in allowed}
    should_stamp_filed = bool(data.get("arn_number")) or data.get("status") == "Filed"

    conn = tdb.get_connection()
    try:
        lead = tdb.set_lead_fields(conn, lead_id, set_data, allowed)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        if should_stamp_filed:
            conn.execute("UPDATE tm_leads SET filed_at = now() WHERE id = %s AND filed_at IS NULL", (lead_id,))
            conn.commit()
            lead = tdb.get_lead(conn, lead_id)
        tdb.log_activity(conn, lead_id, "Status Update", f"Tracking updated: {data.get('mark_status', lead.get('mark_status') or 'ARN/App No set')}")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>/update-objection", methods=["POST"])
def lead_update_objection(lead_id):
    data = _json()
    conn = tdb.get_connection()
    try:
        fields = dict(data)
        if fields.get("reply_filed"):
            conn.execute("UPDATE tm_leads SET reply_filed_at = now() WHERE id = %s", (lead_id,))
            conn.commit()
        allowed = {"hearing_date", "reply_status", "status"}
        lead = tdb.set_lead_fields(conn, lead_id, {k: v for k, v in fields.items() if k in allowed}, allowed)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Status Update", "Objection/opposition update logged")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>/update-publication", methods=["POST"])
def lead_update_publication(lead_id):
    data = _json()
    conn = tdb.get_connection()
    try:
        allowed = {"journal_number", "journal_publication_path", "status"}
        set_data = {k: v for k, v in data.items() if k in allowed}
        conn.execute("UPDATE tm_leads SET journal_published_at = now() WHERE id = %s", (lead_id,))
        conn.commit()
        lead = tdb.set_lead_fields(conn, lead_id, set_data, allowed)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Status Update", f"Published in journal {data.get('journal_number', '')}")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>/update-registration", methods=["POST"])
def lead_update_registration(lead_id):
    data = _json()
    conn = tdb.get_connection()
    try:
        allowed = {"certificate_number", "registration_certificate_path"}
        set_data = {k: v for k, v in data.items() if k in allowed}
        set_data["status"] = "Registered"
        conn.execute("UPDATE tm_leads SET registered_at = now() WHERE id = %s", (lead_id,))
        conn.commit()
        lead = tdb.set_lead_fields(conn, lead_id, set_data, allowed | {"status"})
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Status Update", f"Registered — certificate {data.get('certificate_number', '')}")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/<int:lead_id>/update-invoice", methods=["POST"])
def lead_update_invoice(lead_id):
    data = _json()
    conn = tdb.get_connection()
    try:
        allowed = {"invoice_package_name", "invoice_package_description", "invoice_items", "invoice_remarks", "pricing"}
        set_data = {k: v for k, v in data.items() if k in allowed}
        if "invoice_items" in set_data and not isinstance(set_data["invoice_items"], str):
            set_data["invoice_items"] = json.dumps(set_data["invoice_items"])
        lead = tdb.set_lead_fields(conn, lead_id, set_data, allowed)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Invoice", "Invoice details updated")
    finally:
        conn.close()
    return jsonify(lead)


@bp.route("/leads/kanban/update", methods=["POST"])
def leads_kanban_update():
    """Drag-drop stage change. Since kanban_stage is computed rather than
    stored, dropping a lead onto a column just sets the underlying `status`
    value that maps to that stage — the same rule table
    compute_kanban_stage reads from, so the two can never drift apart."""
    data = _json()
    lead_id = data.get("lead_id")
    stage = data.get("stage")
    if not lead_id or stage not in tdb.KANBAN_STAGES:
        return jsonify({"error": "lead_id and a valid stage are required"}), 400

    stage_to_status = {
        "Registered": "Registered",
        "Exam Report": "Examination Report File",
        "Exam Pending": "Pending for Examination",
        "TM-M Filed": "TM-M File",
        "Hearing": "Hearing",
        "Advertised": "Accepted & Advertised",
        "Reply Filed": "Reply Filed",
        "Objection/Opp.": "Objection",
        "Formality Fail": "Formality Check Fail",
        "Formality Pass": "Formality Check Pass",
        "Marked for Exam": "Marked for Exam",
        "Recently Applied": "Recently Applied",
        "App Filed": "Filed",
    }

    conn = tdb.get_connection()
    try:
        if stage in stage_to_status:
            lead = tdb.update_lead(conn, lead_id, {"status": stage_to_status[stage]})
        elif stage == "Payment Done":
            lead = tdb.set_lead_fields(conn, lead_id, {"payment_status": "Payment Done"}, {"payment_status"})
        else:
            lead = tdb.get_lead(conn, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        tdb.log_activity(conn, lead_id, "Status Update", f"Moved to {stage}")
    finally:
        conn.close()
    return jsonify(lead)


# ─── Consultants ───────────────────────────────────────────────────────────

@bp.route("/consultants", methods=["GET"])
def consultants_list():
    conn = tdb.get_connection()
    try:
        rows = tdb.list_consultants(conn)
    finally:
        conn.close()
    return jsonify({"consultants": rows})


@bp.route("/consultants", methods=["POST"])
def consultants_create():
    conn = tdb.get_connection()
    try:
        row = tdb.create_consultant(conn, _json())
    finally:
        conn.close()
    return jsonify(row), 201


@bp.route("/consultants/<consultant_id>", methods=["PUT"])
def consultants_update(consultant_id):
    conn = tdb.get_connection()
    try:
        row = tdb.update_consultant(conn, consultant_id, _json())
        if not row:
            return jsonify({"error": "Consultant not found"}), 404
    finally:
        conn.close()
    return jsonify(row)


@bp.route("/consultants/<consultant_id>", methods=["DELETE"])
def consultants_delete(consultant_id):
    conn = tdb.get_connection()
    try:
        tdb.delete_consultant(conn, consultant_id)
    finally:
        conn.close()
    return jsonify({"ok": True})


# ─── Packages ──────────────────────────────────────────────────────────────

@bp.route("/packages", methods=["GET"])
def packages_list():
    conn = tdb.get_connection()
    try:
        rows = tdb.list_packages(conn)
    finally:
        conn.close()
    return jsonify({"packages": rows})


@bp.route("/packages", methods=["POST"])
def packages_create():
    conn = tdb.get_connection()
    try:
        row = tdb.create_package(conn, _json())
    finally:
        conn.close()
    return jsonify(row), 201


@bp.route("/packages/<package_id>", methods=["PUT"])
def packages_update(package_id):
    conn = tdb.get_connection()
    try:
        row = tdb.update_package(conn, package_id, _json())
        if not row:
            return jsonify({"error": "Package not found"}), 404
    finally:
        conn.close()
    return jsonify(row)


@bp.route("/packages/<package_id>", methods=["DELETE"])
def packages_delete(package_id):
    conn = tdb.get_connection()
    try:
        tdb.delete_package(conn, package_id)
    finally:
        conn.close()
    return jsonify({"ok": True})


# ─── Settings ──────────────────────────────────────────────────────────────

@bp.route("/settings", methods=["GET"])
def settings_get():
    conn = tdb.get_connection()
    try:
        settings = tdb.get_settings(conn)
    finally:
        conn.close()
    return jsonify(settings)


@bp.route("/settings", methods=["POST"])
def settings_update():
    conn = tdb.get_connection()
    try:
        settings = tdb.update_settings(conn, _json())
    finally:
        conn.close()
    return jsonify(settings)


# ─── Dashboard ─────────────────────────────────────────────────────────────

@bp.route("/dashboard", methods=["GET"])
def dashboard():
    conn = tdb.get_connection()
    try:
        stats = tdb.dashboard_stats(conn)
    finally:
        conn.close()
    return jsonify(stats)
