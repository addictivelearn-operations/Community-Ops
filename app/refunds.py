"""
Community refund: approve one row.

  1. Zoho: create a ticket in "No Reply - SA & LS", send the learner's email
     on it from noreply@, close it Unassigned. Stamp sent_at/sent_by/result.
  2. Team handoff: a row in the finance tracker, a Google Doc, the internal
     email. Stamp handoff.

Same sequencing and stamps as RF_02_Mailer.gs / RF_04_TeamHandoff.gs — the
ONE deliberate difference is that the finance email is sent by the signed-in
approver through their own Gmail, the thing Apps Script's trigger model could
never do.

State for retries lives in the finance tracker itself (Source Key in AB, doc
link in P, email status in Q — that sheet is unaffected by the tracker/
Community Refund migration, see HANDOVER.txt §6), so a retry only does what
is still missing.
"""

import html as htmlmod
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import tracker_app, zoho
from .config import TEAM, TEAM_HEADERS, settings
from .config import RF_CURRENCY, RF_SUBJECT, RF_SUBJECT_NO_ORDER, RF_TEMPLATE, RF_TEMPLATE_NO_ORDER
from .google import GoogleClient, GoogleError
from .store import RefundRow, load_refund, now_stamp, write_refund

# col_letter/cell are still needed for the finance-team tracker, which stays
# a Google Sheet (HANDOVER.txt §6) — kept here rather than in store.py, which
# is now purely SQLite-backed.


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def cell(row: list, col: int) -> str:
    v = row[col - 1] if len(row) >= col else ""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


@dataclass
class Outcome:
    ok: bool
    message: str


# ---------------------------------------------------------------------------
# Learner email (same markup rules as the Apps Script template)
# ---------------------------------------------------------------------------

def esc(s) -> str:
    return htmlmod.escape("" if s is None else str(s), quote=True)


def format_amount(amount) -> str:
    s = "" if amount is None else str(amount).strip()
    if not s or RF_CURRENCY in s:
        return s
    return RF_CURRENCY + s


def _markup(escaped: str) -> str:
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                     r'<a href="\2" target="_blank">\1</a>', escaped)
    return re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", escaped)


def learner_email_html(r: RefundRow) -> str:
    template = RF_TEMPLATE_NO_ORDER if r.no_order else RF_TEMPLATE
    filled = (template.replace("{{name}}", r.name)
              .replace("{{amount}}", format_amount(r.amount))
              .replace("{{community}}", r.community))
    paras = []
    for p in re.split(r"\n\s*\n", filled):
        paras.append("<p>" + _markup(esc(p.strip())).replace("\n", "<br>") + "</p>")
    return ('<div style="font-family: Arial, sans-serif; font-size: 14px; '
            'color: #333333; line-height: 1.6;">' + "".join(paras) + "</div>")


def learner_subject(r: RefundRow) -> str:
    return RF_SUBJECT_NO_ORDER if r.no_order else RF_SUBJECT


# ---------------------------------------------------------------------------
# Step 1 — Zoho
# ---------------------------------------------------------------------------

STRANDED = re.compile(r"Ticket #(\S+) created but the email FAILED \(id (\d+)\)")


def send_learner_email(r: RefundRow, approver_name: str, allow_resend: bool = False) -> Outcome:
    if r.sent and not allow_resend:
        return Outcome(False, f"Already sent on {r.sent_at.splitlines()[0]} — use Send again to re-send.")
    blockers = r.blockers(handoff_on=True)
    if blockers:
        return Outcome(False, "Missing: " + ", ".join(blockers))

    subject = learner_subject(r)
    body = learner_email_html(r)

    # Reuse the ticket a previous attempt created, rather than leaving a
    # duplicate in Desk for the same refund. Only matches a STRANDED failure
    # message, never a past success, so a deliberate resend always gets its
    # own new ticket — the old one is already closed.
    m = STRANDED.search(r.result or "")
    if m:
        ticket_number, ticket_id = m.group(1), m.group(2)
    else:
        try:
            t = zoho.create_ticket(subject=subject, department_id=settings.rf_department_id,
                                   name=r.name, email=r.email, phone=r.phone or None)
        except zoho.ZohoError as e:
            return Outcome(False, f"Ticket creation failed: {e}")
        ticket_id = str(t["id"])
        ticket_number = str(t.get("ticketNumber") or ticket_id)

    try:
        zoho.send_reply(ticket_id, r.email, settings.rf_from_address, body)
    except zoho.ZohoError as e:
        msg = f"Ticket #{ticket_number} created but the email FAILED (id {ticket_id}): {e}"
        write_refund(r.row, "result", "❌ " + msg)
        return Outcome(False, msg)

    # Stamp BEFORE closing — a failed close must never cause a second email.
    # Stacked newest-first, same as the ticket-reply resend, so a resend
    # never loses the original send's timestamp.
    line = f"{now_stamp()} — {approver_name}"
    write_refund(r.row, "sent_at", f"{line}\n{r.sent_at}" if r.sent_at else line)
    write_refund(r.row, "sent_by", approver_name)

    message = (f"#{ticket_number} Re-sent" if r.sent else f"#{ticket_number} Email sent")
    try:
        zoho.close_ticket(ticket_id, unassign=True)
        message += " & closed"
    except zoho.ZohoError as e:
        message += f" — closing FAILED: {e}"
    return Outcome(True, message)


def resend_learner_email(row: int, approver_name: str) -> Outcome:
    """Deliberately re-send the learner's refund email on a row that was
    already sent once — a brand new Zoho ticket, exactly like the first
    send. Unlike the Trigger dropdown (which never re-sends), this is the
    explicit "Send again" action. Never touches the team handoff, which has
    its own separate Retry button and its own idempotency via Source Key."""
    r = load_refund(row)
    if not r:
        return Outcome(False, f"Row {row} is empty")
    if not r.sent:
        return Outcome(False, "Not sent yet — use the Trigger dropdown to send it the first time.")
    out = send_learner_email(r, approver_name, allow_resend=True)
    write_refund(row, "result", ("✅ " if out.ok else "❌ ") + out.message)
    return out


# ---------------------------------------------------------------------------
# Step 2 — team handoff
# ---------------------------------------------------------------------------

@dataclass
class Handoff:
    key: str
    name: str
    email: str
    phone: str
    community: str
    reason: str
    amount: str
    brand: str
    approved_by: str
    approved_by_email: str
    approved_at: datetime
    deadline: datetime


def handoff_data(r: RefundRow, approver_name: str, approver_email: str) -> Handoff:
    approved_by = r.sent_by or approver_name
    approved_by_email = ""
    for addr, name in settings.authorized_senders.items():
        if name == approved_by:
            approved_by_email = addr
            break
    now = datetime.now(settings.tz)
    return Handoff(
        key=r.key, name=r.name, email=r.email, phone=r.phone, community=r.community,
        reason=r.reason, amount=r.amount, brand=r.brand,
        approved_by=approved_by, approved_by_email=approved_by_email or approver_email,
        approved_at=now, deadline=now + timedelta(days=settings.team_deadline_days),
    )


def _stamp(d: datetime) -> str:
    return d.strftime("%d/%m/%Y, %I:%M %p")


def _sheet_date(d: datetime) -> str:
    """USER_ENTERED text Sheets parses back into a real date cell."""
    return d.strftime("%d/%m/%Y %H:%M:%S")


def _tracker_last_row(g: GoogleClient) -> int:
    names = g.sheet_values(settings.team_sheet_id, settings.team_tab,
                           f"{col_letter(TEAM.NAME)}2:{col_letter(TEAM.NAME)}")
    last = 1
    for i, row in enumerate(names):
        if cell(row, 1):
            last = i + 2
    return last


def _ensure_tracker(g: GoogleClient) -> None:
    tabs = {t["title"]: t for t in g.sheet_tabs(settings.team_sheet_id)}
    if settings.team_tab not in tabs:
        g.sheet_add_tab(settings.team_sheet_id, settings.team_tab)
    first = g.sheet_values(settings.team_sheet_id, settings.team_tab, "A1:A1")
    if not first or not cell(first[0], 1):
        g.sheet_write(settings.team_sheet_id, settings.team_tab,
                      f"A1:{col_letter(len(TEAM_HEADERS))}1", [TEAM_HEADERS])


def _find_tracker_row(g: GoogleClient, key: str) -> int:
    last = _tracker_last_row(g)
    if last < 2:
        return 0
    col = col_letter(TEAM.SOURCE_KEY)
    keys = g.sheet_values(settings.team_sheet_id, settings.team_tab, f"{col}2:{col}{last}")
    for i, row in enumerate(keys):
        if cell(row, 1) == key:
            return i + 2
    return 0


def _append_tracker_row(g: GoogleClient, d: Handoff) -> int:
    row = max(_tracker_last_row(g) + 1, settings.team_first_auto_row)
    values = [""] * len(TEAM_HEADERS)
    values[TEAM.TIMESTAMP - 1] = _sheet_date(d.approved_at)
    values[TEAM.APPROVER_EMAIL - 1] = d.approved_by_email
    values[TEAM.BRAND - 1] = d.brand
    values[TEAM.NAME - 1] = d.name
    values[TEAM.EMAIL - 1] = d.email
    values[TEAM.PHONE - 1] = d.phone
    values[TEAM.PRODUCT - 1] = d.community
    values[TEAM.REASON - 1] = d.reason
    values[TEAM.APPROVED_BY - 1] = d.approved_by
    values[TEAM.AMOUNT - 1] = d.amount
    values[TEAM.DEADLINE - 1] = _sheet_date(d.deadline)
    values[TEAM.SOURCE_KEY - 1] = d.key
    # USER_ENTERED so the two dates become real date cells; the leading
    # apostrophe keeps a numeric phone as text ("+91…" and leading zeros).
    if d.phone and d.phone.replace("+", "").isdigit():
        values[TEAM.PHONE - 1] = "'" + d.phone
    g.sheet_write(settings.team_sheet_id, settings.team_tab,
                  f"A{row}:{col_letter(len(TEAM_HEADERS))}{row}", [values], raw=False)
    return row


def _tracker_url(g: GoogleClient, row: int) -> str:
    gid = 0
    for t in g.sheet_tabs(settings.team_sheet_id):
        if t["title"] == settings.team_tab:
            gid = t["sheetId"]
    return (f"https://docs.google.com/spreadsheets/d/{settings.team_sheet_id}"
            f"/edit#gid={gid}&range=A{row}")


def doc_html(d: Handoff, tracker_url: str) -> str:
    def section(title, rows):
        trs = "".join(f"<tr><td><b>{esc(k)}</b></td><td>{esc(v) or 'Details not updated'}</td></tr>"
                      for k, v in rows)
        return (f"<h1>{esc(title)}</h1><table border='1' cellpadding='4' "
                f"style='border-collapse:collapse'>{trs}</table>")
    title = settings.team_doc_title_prefix + d.name
    return (
        f"<html><body style='font-family:Arial'><h1 style='font-size:20pt'>{esc(title)}</h1>"
        f"<p><i>Generated on: {esc(_stamp(datetime.now(settings.tz)))}</i></p>"
        f"<p>Generated when the refund was approved. The tracker row is the live record: "
        f"<a href='{esc(tracker_url)}'>{esc(tracker_url)}</a></p>"
        + section("Payee Details", [("Brand", d.brand), ("Payee Name", d.name),
                                    ("Payee Email", d.email), ("Payee Number", d.phone),
                                    ("Product Purchased", d.community)])
        + section("Refund Details", [("Reason For Refund", d.reason),
                                     ("Refund Approved By", d.approved_by),
                                     ("Refund Amount", format_amount(d.amount)),
                                     ("Refund Should Ideally Be Processed by", _stamp(d.deadline))])
        + section("Banking Details", [("Bank Name", ""), ("Bank Account Holder Name", ""),
                                      ("Bank Account Number", ""), ("Bank IFSC Code", ""),
                                      ("Finance/Loan/Payment Partner", "")])
        + section("Action Taken by Finance Team", [("Actual Amount to be refunded", ""),
                                                   ("Refund Processed?", ""),
                                                   ("Refund Processed By", ""),
                                                   ("Refund Processed Timestamp", ""),
                                                   ("Comments", "")])
        + "</body></html>"
    )


def team_email_html(d: Handoff, doc_url: str) -> str:
    def row(label, value):
        return f"<tr><td><strong>{label}</strong></td><td>{esc(value)}</td></tr>"
    table = ('<table border="1" cellspacing="0" cellpadding="5" '
             'style="border-collapse: collapse; width: 100%; font-size: 14px;">')

    def h3(colour, text):
        return f'<h3 style="color: white; background-color: {colour}; padding: 10px;">{text}</h3>'

    doc_section = ""
    if doc_url:
        doc_section = (h3("#D35400", "Refund Approval Document") +
                       "<p>The Refund Approval Document records this approval. The tracker "
                       "row is the live record of what the Finance team does with it.</p>"
                       f'<p><a href="{esc(doc_url)}" target="_blank">View Refund Approval Document</a></p>')
    amount = format_amount(d.amount)
    return (
        '<div style="font-family: Arial, sans-serif; max-width: 700px; padding: 20px; background-color: #f9f9f9;">'
        '<div style="background-color: white; padding: 20px; border: 1px solid #d1d1d1;">'
        "<p><strong>Dear Team,</strong></p>"
        "<p>A community refund has been approved for the learner with the following details:</p>"
        + h3("#4A90E2", "Payee Details") + table
        + (row("Brand", d.brand) if d.brand else "")
        + row("Payee Name", d.name) + row("Payee Email", d.email)
        + row("Payee Number", d.phone) + row("Product Purchased", d.community)
        + "</table>"
        + h3("#27AE60", "Refund Details") + table
        + row("Reason for Refund", d.reason) + row("Refund Approved By", d.approved_by)
        + row("Refund Amount", amount)
        + row("Refund Should Ideally Be Processed by", _stamp(d.deadline))
        + "</table>"
        + doc_section
        + h3("#F5A623", "Next Steps") + table
        + "<tr><td><strong>Finance Team</strong></td><td>"
        f"The amount recorded for this refund is <strong>{esc(amount)}</strong>. Please verify and "
        "confirm the final refund amount in <strong>Column T</strong> of the tracker. Once processed, "
        "update <strong>Column U</strong>.</td></tr></table>"
        '<p style="margin-top: 20px; font-size: 16px; font-weight: bold; color: #2C3E50;">Regards,</p>'
        f'<p style="font-size: 16px; font-weight: bold; color: #2980B9;">{esc(settings.team_signature)}</p>'
        "</div></div>"
    )


def recipients() -> dict:
    return {"to": settings.team_to, "cc": settings.team_cc, "bcc": settings.team_bcc}


def _tracker_app_values(d: Handoff) -> dict:
    """The finance sheet's row, by its own column names — what _append_tracker_row
    writes, sent to the tracker app instead (Hardik, 21 Sep 2026: "whatever data
    you are sending to the sheet, send it as it is")."""
    h = TEAM_HEADERS
    return {
        h[TEAM.TIMESTAMP - 1]: _sheet_date(d.approved_at),
        h[TEAM.APPROVER_EMAIL - 1]: d.approved_by_email,
        h[TEAM.BRAND - 1]: d.brand,
        h[TEAM.NAME - 1]: d.name,
        h[TEAM.EMAIL - 1]: d.email,
        h[TEAM.PHONE - 1]: d.phone,
        h[TEAM.PRODUCT - 1]: d.community,
        h[TEAM.REASON - 1]: d.reason,
        h[TEAM.APPROVED_BY - 1]: d.approved_by,
        h[TEAM.AMOUNT - 1]: d.amount,
        h[TEAM.DEADLINE - 1]: _sheet_date(d.deadline),
        h[TEAM.SOURCE_KEY - 1]: d.key,
    }


def run_handoff_to_tracker_app(g: GoogleClient, r: RefundRow, d: Handoff) -> Outcome:
    """Steps 1–3 against the Master Refund Tracker app instead of the finance
    sheet (Hardik, 21 Sep 2026). Same order, same never-twice rules: the row
    is found by Source Key, the Doc is made only when the row has none, the
    e-mail goes only when the row does not say Sent. The Doc and the e-mail
    are unchanged except that their tracker link opens the learner in the
    app (Phase 4 → Community Refund)."""
    parts = []
    h = TEAM_HEADERS
    # 1. The row
    try:
        st = tracker_app.upsert(d.key, _tracker_app_values(d), d.approved_by, r.row)
    except tracker_app.TrackerError as e:
        return Outcome(False, f"Tracker app: {e}")
    tracker_row = int(st.get("rowNumber") or 0)
    parts.append(f"tracker app row {tracker_row}" + ("" if st.get("created") else " (already there)"))
    tracker_url = st.get("url") or settings.tracker_url

    # 2. Doc — only if the row has none
    doc_url = st.get("docLink") or ""
    if not doc_url:
        try:
            created = g.create_doc_from_html(settings.team_doc_title_prefix + d.name,
                                             doc_html(d, tracker_url), settings.team_doc_folder_id)
            doc_url = created.get("webViewLink") or f"https://docs.google.com/document/d/{created['id']}/edit"
            warn = g.share_anyone_view(created["id"])
            tracker_app.update(d.key, {h[TEAM.DOC_LINK - 1]: doc_url}, d.approved_by, "approval document created")
            parts.append("doc created" + (f" ({warn})" if warn else ""))
        except (GoogleError, tracker_app.TrackerError) as e:
            return Outcome(False, " · ".join(parts) + f" · doc FAILED: {e}")

    # 3. E-mail — only if the row does not say Sent
    if (st.get("emailStatus") or "") == "Sent":
        parts.append("email already sent")
    else:
        rc = recipients()
        subject = f"Refund Approved for {d.name}"
        try:
            g.send_mail(from_name="Refund Approval Alert", to=rc["to"], cc=rc["cc"], bcc=rc["bcc"],
                        subject=subject, html=team_email_html(d, doc_url),
                        reply_to=d.approved_by_email or None)
            tracker_app.update(d.key, {h[TEAM.MAIL_STATUS - 1]: "Sent",
                                       h[TEAM.MAIL_SENT_AT - 1]: _sheet_date(datetime.now(settings.tz))},
                               d.approved_by, "finance e-mail sent")
            parts.append("email sent to " + ", ".join(rc["to"]) + f" from {g.email}")
        except (GoogleError, tracker_app.TrackerError) as e:
            return Outcome(False, " · ".join(parts) + f" · email FAILED: {e}")

    return Outcome(True, " · ".join(parts))


def run_handoff(g: GoogleClient, r: RefundRow, approver_name: str, approver_email: str) -> Outcome:
    if r.no_order:
        return Outcome(True, 'Skipped — "No order found" row, nothing for the team')
    if not r.name or not r.email:
        return Outcome(False, "Name or email missing on the row")
    d = handoff_data(r, approver_name, approver_email)
    # The finance row goes to the Master Refund Tracker app when it is
    # configured (Hardik, 21 Sep 2026); the sheet path below stays as the
    # fallback and is untouched.
    if tracker_app.configured():
        return run_handoff_to_tracker_app(g, r, d)
    parts = []

    # 1. Tracker row
    try:
        _ensure_tracker(g)
        tracker_row = _find_tracker_row(g, d.key)
        if tracker_row:
            parts.append(f"tracker row {tracker_row} (already there)")
        else:
            tracker_row = _append_tracker_row(g, d)
            parts.append(f"tracker row {tracker_row}")
    except GoogleError as e:
        return Outcome(False, f"Tracker: {e}")
    tracker_url = _tracker_url(g, tracker_row)

    # 2. Doc — only if the tracker row has none
    doc_col = col_letter(TEAM.DOC_LINK)
    existing = g.sheet_values(settings.team_sheet_id, settings.team_tab, f"{doc_col}{tracker_row}")
    doc_url = cell(existing[0], 1) if existing else ""
    if not doc_url:
        try:
            created = g.create_doc_from_html(settings.team_doc_title_prefix + d.name,
                                             doc_html(d, tracker_url), settings.team_doc_folder_id)
            doc_url = created.get("webViewLink") or f"https://docs.google.com/document/d/{created['id']}/edit"
            warn = g.share_anyone_view(created["id"])
            g.sheet_write(settings.team_sheet_id, settings.team_tab, f"{doc_col}{tracker_row}", [[doc_url]])
            parts.append("doc created" + (f" ({warn})" if warn else ""))
        except GoogleError as e:
            return Outcome(False, " · ".join(parts) + f" · doc FAILED: {e}")

    # 3. Email — only if the tracker row does not say Sent
    st_col = col_letter(TEAM.MAIL_STATUS)
    st = g.sheet_values(settings.team_sheet_id, settings.team_tab, f"{st_col}{tracker_row}")
    if st and cell(st[0], 1) == "Sent":
        parts.append("email already sent")
    else:
        rc = recipients()
        subject = f"Refund Approved for {d.name}"
        try:
            g.send_mail(from_name="Refund Approval Alert", to=rc["to"], cc=rc["cc"], bcc=rc["bcc"],
                        subject=subject, html=team_email_html(d, doc_url),
                        reply_to=d.approved_by_email or None)
            g.sheet_write(settings.team_sheet_id, settings.team_tab,
                          f"{st_col}{tracker_row}:{col_letter(TEAM.MAIL_SENT_AT)}{tracker_row}",
                          [["Sent", _sheet_date(datetime.now(settings.tz))]], raw=False)
            parts.append("email sent to " + ", ".join(rc["to"]) + f" from {g.email}")
        except GoogleError as e:
            return Outcome(False, " · ".join(parts) + f" · email FAILED: {e}")

    return Outcome(True, " · ".join(parts))


def handoff_and_record(g: GoogleClient, r: RefundRow, approver_name: str, approver_email: str) -> Outcome:
    try:
        out = run_handoff(g, r, approver_name, approver_email)
    except Exception as e:  # noqa: BLE001 — recorded, never raised past here
        out = Outcome(False, str(e))
    write_refund(r.row, "handoff", ("✅ " if out.ok else "❌ ") + out.message)
    return out


# ---------------------------------------------------------------------------
# The whole approval
# ---------------------------------------------------------------------------

def approve(g: GoogleClient, row: int, approver_name: str, approver_email: str) -> tuple[Outcome, Outcome | None]:
    """Re-reads the row first: another agent may be acting on the same row
    at the same time, and their stamp must win over a stale page. `g` is
    only needed downstream, for the finance-team handoff."""
    r = load_refund(row)
    if not r:
        return Outcome(False, f"Row {row} is empty"), None
    if r.trigger.lower() != "yes":
        write_refund(row, "trigger", "Yes")     # the app IS the approval
    first = send_learner_email(r, approver_name)
    write_refund(row, "result", ("✅ " if first.ok else "❌ ") + first.message)
    if not first.ok:
        return first, None
    fresh = load_refund(row) or r
    second = handoff_and_record(g, fresh, approver_name, approver_email)
    return first, second
