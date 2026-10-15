"""
Sends the daily digest email — one email per user, covering status changes,
renewals due soon, and compliance notices (examination report replies,
Section 132 replies, hearings, opposition counter-statements) grouped from
real portal reminder/correspondence data. Plain SMTP over a real Gmail
account + app password (see config.DIGEST_EMAIL_ADDRESS/APP_PASSWORD), not
a third-party email API — this app already leans on Gmail for the
scraper's OTP pool, so reusing the same kind of credential here needs no
new service/dependency.

Some due dates shown here are computed by this app (notice date + a
best-effort offset), not published by the portal — those are always
labeled "(Est.)" so they're never mistaken for an official deadline. See
db.list_compliance_notices for exactly which dates are real vs. estimated.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config

log = logging.getLogger("digest_email")

_COMPLIANCE_SECTION_TITLES = {
    "examination_report_reply": "Examination Report Reply",
    "reply_us_132": "Reply to Exam Report U/s 132",
    "hearing_show_cause": "Hearing (Show Cause)",
    "counter_statement": "Counter Statement",
    "hearing_opposition": "Hearing (Opposition)",
    "evidence_rule_45": "Evidence — Rule 45",
    "evidence_rule_46": "Evidence — Rule 46",
    "evidence_rule_47": "Evidence — Rule 47",
    "application_accepted": "Application accepted",
}


def _due_badge(days_until: int) -> str:
    if days_until < 0:
        return f"<span style='background:#FBE7E7;color:#C23B3B;border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600;'>Overdue</span>"
    return f"<span style='background:#EAF1FF;color:#2452C8;border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600;'>{days_until} day{'s' if days_until != 1 else ''} left</span>"


def _compliance_item_html(item: dict) -> str:
    due_label = item["due_date"] + (" (Est.)" if item.get("is_estimated") else "")
    name = item.get("word_mark") or item.get("label") or item.get("app_no")
    subject = (item.get("subject") or "")[:140]
    return f"""
    <div style="padding:10px 12px;border-bottom:1px solid #e4e8f1;">
      <div style="display:flex;justify-content:space-between;gap:8px;">
        <div>
          <div style="font-family:monospace;color:#5A6683;font-size:11.5px;">#{item.get('app_no')}</div>
          <div style="font-weight:600;font-size:13px;">{name}</div>
          <div style="color:#5A6683;font-size:11.5px;margin-top:2px;">{subject}</div>
        </div>
        <div style="text-align:right;flex-shrink:0;">
          {_due_badge(item['days_until'])}
          <div style="color:#5A6683;font-size:11px;margin-top:4px;">{due_label}</div>
        </div>
      </div>
    </div>
    """


def _compliance_section_html(category: str, items: list[dict]) -> str:
    if not items:
        return ""
    title = _COMPLIANCE_SECTION_TITLES.get(category, category)
    rows = "".join(_compliance_item_html(i) for i in items)
    return f"""
    <div style="margin-top:18px;border:1px solid #e4e8f1;border-radius:8px;overflow:hidden;">
      <div style="background:#F5F7FB;padding:8px 12px;font-weight:600;font-size:13px;">{title} <span style="color:#5A6683;font-weight:400;">({len(items)} item{'s' if len(items) != 1 else ''})</span></div>
      {rows}
    </div>
    """


def _status_changes_html(changes: list[dict]) -> str:
    if not changes:
        return ""
    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;'>{c['word_mark']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;font-family:monospace;'>#{c['app_no']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;color:#5A6683;'>{c['old_status']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;font-weight:600;'>{c['new_status']}</td>"
        f"</tr>"
        for c in changes
    )
    return f"""
    <h3 style="font-size:14px;margin:18px 0 8px;">Status updates</h3>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      <thead>
        <tr style="text-align:left;color:#5A6683;">
          <th style="padding:8px 12px;">Word mark</th>
          <th style="padding:8px 12px;">App number</th>
          <th style="padding:8px 12px;">Old status</th>
          <th style="padding:8px 12px;">New status</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """


def _renewals_html(renewals: list[dict]) -> str:
    if not renewals:
        return ""
    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;'>{r.get('word_mark') or r.get('label') or r['app_no']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;font-family:monospace;'>#{r['app_no']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;'>{r['due_date']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e4e8f1;'>{_due_badge(r['days_until'])}</td>"
        f"</tr>"
        for r in renewals
    )
    return f"""
    <h3 style="font-size:14px;margin:18px 0 8px;">Renewals due soon</h3>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      <thead>
        <tr style="text-align:left;color:#5A6683;">
          <th style="padding:8px 12px;">Word mark</th>
          <th style="padding:8px 12px;">App number</th>
          <th style="padding:8px 12px;">Valid upto</th>
          <th style="padding:8px 12px;"></th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """


def _count_pill(label: str, n: int) -> str:
    return f"<span style='background:#F5F7FB;border-radius:999px;padding:4px 10px;font-size:12px;margin-right:6px;'>{label}: <b>{n}</b></span>"


def _build_message(
    to_email: str,
    status_changes: list[dict],
    compliance: dict[str, list[dict]],
    renewals: list[dict],
    new_applications_count: int,
) -> MIMEMultipart:
    compliance_total = sum(len(v) for v in compliance.values())
    overdue_total = sum(1 for items in compliance.values() for i in items if i["days_until"] < 0) + \
        sum(1 for r in renewals if r["days_until"] < 0)
    due_soon_7d = sum(1 for items in compliance.values() for i in items if 0 <= i["days_until"] <= 7) + \
        sum(1 for r in renewals if 0 <= r["days_until"] <= 7)

    subject = f"TM Tracker — {len(status_changes)} status update{'s' if len(status_changes) != 1 else ''}, {compliance_total} compliance item{'s' if compliance_total != 1 else ''} today"

    lines_text = [f"{len(status_changes)} status update(s), {compliance_total} compliance item(s), {len(renewals)} renewal(s) due soon."]
    for c in status_changes:
        lines_text.append(f"- {c['word_mark']} (#{c['app_no']}): {c['old_status']} -> {c['new_status']}")
    for category, items in compliance.items():
        for i in items:
            lines_text.append(f"- [{_COMPLIANCE_SECTION_TITLES.get(category, category)}] {i.get('word_mark') or i['app_no']}: due {i['due_date']}{' (needs confirmation)' if i['is_estimated'] else ''}")
    text_body = "\n".join(lines_text)

    counts_html = (
        _count_pill("Status updates", len(status_changes))
        + _count_pill("Compliance", compliance_total)
        + _count_pill("Due soon (7d)", due_soon_7d)
        + _count_pill("Overdue", overdue_total)
        + _count_pill("New applications (24h)", new_applications_count)
    )

    compliance_html = "".join(_compliance_section_html(cat, items) for cat, items in compliance.items())

    html_body = f"""
    <div style="font-family:-apple-system,'Segoe UI',sans-serif;color:#0C1530;max-width:640px;">
      <h2 style="font-size:17px;margin-bottom:8px;">TM Tracker daily update</h2>
      <div style="margin-bottom:8px;">{counts_html}</div>
      {_status_changes_html(status_changes)}
      {compliance_html}
      {_renewals_html(renewals)}
      <p style="color:#8A93A8;font-size:11px;margin-top:20px;">Dates marked "(Est.)" are computed by TM Tracker from the notice date plus a typical reply window — not an official deadline published by the portal. Always confirm against the original notice.</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.DIGEST_EMAIL_ADDRESS
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


def send_digest(
    to_email: str,
    status_changes: list[dict],
    compliance: dict[str, list[dict]] | None = None,
    renewals: list[dict] | None = None,
    new_applications_count: int = 0,
) -> bool:
    """Returns True on success. Never raises — a failed send for one user
    must not crash the digest job for everyone else (see scheduler's
    caller, which logs and moves on)."""
    compliance = compliance or {}
    renewals = renewals or []
    if not config.is_digest_email_configured():
        log.warning("Digest email not configured (DIGEST_EMAIL_ADDRESS/APP_PASSWORD unset) — skipping send to %s", to_email)
        return False
    if not status_changes and not any(compliance.values()) and not renewals:
        return True

    msg = _build_message(to_email, status_changes, compliance, renewals, new_applications_count)
    try:
        with smtplib.SMTP_SSL(config.DIGEST_SMTP_HOST, config.DIGEST_SMTP_PORT, timeout=20) as server:
            server.login(config.DIGEST_EMAIL_ADDRESS, config.DIGEST_EMAIL_APP_PASSWORD)
            server.sendmail(config.DIGEST_EMAIL_ADDRESS, [to_email], msg.as_string())
        return True
    except Exception:
        log.exception("Failed to send digest email to %s", to_email)
        return False
