"""
Ticket replies: post the resolution as a reply on the ticket's own Zoho
thread, close the ticket, stamp sent_at/sent_by/result. A port of
09_ZohoReply.gs — same wording, same fields, same "stamp before close"
ordering.
"""

import html as htmlmod
from dataclasses import dataclass

from . import zoho
from .config import settings
from .store import TicketRow, load_ticket, now_stamp, ticket_id_for_number, write_main


@dataclass
class Outcome:
    ok: bool
    message: str


def esc(s) -> str:
    return htmlmod.escape("" if s is None else str(s), quote=True).replace("\n", "<br>")


def from_address(brand: str) -> str:
    key = (brand or "").strip().lower()
    m = settings.reply_from_addresses
    if key in m:
        return m[key]
    squashed = key.replace(" ", "")
    for k, v in m.items():
        if k.replace(" ", "") == squashed:
            return v
    return settings.reply_default_from


def reply_html(t: TicketRow, resolution: str | None = None) -> str:
    return (
        '<div style="font-family: Arial, sans-serif; font-size: 14px; color: #333333; line-height: 1.6;">'
        f"<p>Dear {esc(t.name)},</p>"
        "<p>Please find below the response to your query raised with Support regarding "
        f"<strong>{esc(t.course)}</strong>:</p>"
        f"<p><strong>Query:</strong> {esc(t.requirement)}</p>"
        f"<p><strong>Response:</strong> {esc(resolution if resolution is not None else t.resolution)}</p>"
        "<br><p>Best regards,<br><strong>Community Team</strong><br>"
        "<strong>Lawsikho &amp; SkillArbitrage</strong></p></div>"
    )


def resolve_ticket_id(ticket_number: str) -> str | None:
    return ticket_id_for_number(ticket_number) or zoho.ticket_id_for_number(ticket_number)


def blockers(t: TicketRow) -> list[str]:
    out = []
    if not t.ticket:
        out.append("ticket number")
    if not t.email:
        out.append("learner email")
    if not t.resolution:
        out.append("resolution")
    if not from_address(t.brand):
        out.append(f'no From address for brand "{t.brand}"')
    return out


def send(row: int, sender_name: str, allow_resend: bool = False) -> Outcome:
    """Re-reads the row first — someone may have sent it since the page was
    loaded, and their stamp must win."""
    t = load_ticket(row)
    if not t:
        return Outcome(False, f"Row {row} is empty")
    if t.sent and not allow_resend:
        return Outcome(False, f"Already sent on {t.last_sent_line} — use Send again to re-send.")
    b = blockers(t)
    if b:
        return Outcome(False, "Missing: " + ", ".join(b))

    ticket_id = resolve_ticket_id(t.ticket)
    if not ticket_id:
        out = Outcome(False, f"Ticket #{t.ticket} not found in Zoho Desk")
        write_main(row, "result", out.message)
        return out

    if t.trigger.lower() != "yes":
        write_main(row, "trigger", "Yes")

    try:
        zoho.send_reply(ticket_id, t.email, from_address(t.brand), reply_html(t))
    except zoho.ZohoError as e:
        out = Outcome(False, f"Reply failed: {e}")
        write_main(row, "result", out.message)
        return out

    # Stamp first (newest line on top), then close.
    line = f"{now_stamp()} — {sender_name}"
    stamps = f"{line}\n{t.sent_at}" if t.sent_at else line
    write_main(row, "sent_at", stamps)
    write_main(row, "sent_by", sender_name)

    message = ("Re-sent on ticket #" if t.sent_at else "Replied on ticket #") + t.ticket
    try:
        zoho.close_ticket(ticket_id)
        message += " and closed"
    except zoho.ZohoError as e:
        message += f" — SENT, but closing failed: {e}"
    write_main(row, "result", message)
    return Outcome(True, message)


def save_resolution(row: int, text: str) -> None:
    write_main(row, "resolution", text)
