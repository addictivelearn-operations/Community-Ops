"""
The ticket tracker and Community Refund tab, backed by the app's own SQLite
database (db.py) instead of the Sheets API. This is the module the README
always said would get swapped for a database — same function shapes as the
old sheets.py, minus the GoogleClient parameter these no longer need.

`row` on each dataclass is kept as the field name for the row's identity
(now a DB-assigned id, not a sheet row number) so templates and routes that
already treat it as an opaque id in a URL don't need to change.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from .config import MAIN, RF, settings
from .db import get_conn

INTERNAL_DOMAINS = {"lawsikho.com", "lawsikho.in", "skillarbitra.ge",
                    "skillarbitrage.ge", "addictivelearn.com", "ipleaders.in"}

TRIGGER_FALLBACK = ["Yes", "Pending", "NA"]
STATUS_FALLBACK = ["Pending", "Approved"]

RF_NO_ORDER = "No order found"

# The array formula Brand used to be, on the Community Refund tab's Column K
# (HANDOVER.txt §3) — keyed on the cleaned community name (Column I).
_SKILLARBITRAGE_RE = re.compile(r"ai for women|women ai|idc|independent director", re.I)


def compute_brand(community: str) -> str:
    if not community:
        return ""
    return "SkillArbitrage" if _SKILLARBITRAGE_RE.search(community) else "LawSikho"


def now_stamp() -> str:
    return datetime.now(settings.tz).strftime("%d/%m/%Y %H:%M:%S")


def now_iso() -> str:
    return datetime.now(settings.tz).isoformat()


def fmt_dt(iso: str) -> str:
    """ISO timestamp (as stored in the DB) → the same display format the
    sheet cells used to show."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%d/%m/%Y %H:%M:%S")


def is_internal(email: str) -> bool:
    return email.rsplit("@", 1)[-1].lower() in INTERNAL_DOMAINS if "@" in email else False


def phone_text(v: str) -> str:
    """Legacy rows imported before the RichTextValue fix hold "+91 …" as a
    formula. Kept for any row migrated from the sheet in that state."""
    if v.startswith("="):
        return v[1:].strip()
    if v == "#ERROR!":
        return "(broken cell — run repairPhoneColumn)"
    return v


# ---------------------------------------------------------------------------
# Ticket tracker
# ---------------------------------------------------------------------------

@dataclass
class TicketRow:
    row: int             # DB id
    ticket: str
    ticket_id: str
    owner: str
    created: str
    imported_at: str
    brand: str
    name: str
    email: str
    phone: str
    course: str
    requirement: str
    resolution: str
    res_status: str
    trigger: str
    sent_at: str
    sent_by: str
    category: str
    result: str
    zoho_status: str
    modified_time: str

    @property
    def sent(self) -> bool:
        return bool(self.sent_at)

    @property
    def pending(self) -> bool:
        return self.trigger.lower() == "yes" and not self.sent

    @property
    def last_sent_line(self) -> str:
        return self.sent_at.split("\n")[0] if self.sent_at else ""


# Which fields an agent may type into — everything else is written by the
# sync, the categoriser, or the send itself.
MAIN_EDITABLE = {"course", "requirement", "resolution", "res_status", "trigger"}


def _ticket_row(r) -> TicketRow:
    return TicketRow(
        row=r["id"], ticket=r["ticket"], ticket_id=r["ticket_id"], owner=r["owner"],
        created=fmt_dt(r["created_at"]), imported_at=fmt_dt(r["imported_at"]),
        brand=r["brand"], name=r["name"], email=r["email"], phone=phone_text(r["phone"]),
        course=r["course"], requirement=r["requirement"], resolution=r["resolution"],
        res_status=r["res_status"], trigger=r["trigger_value"], sent_at=r["sent_at"],
        sent_by=r["sent_by"], category=r["category"], result=r["result"],
        zoho_status=r["zoho_status"], modified_time=r["modified_time"],
    )


def load_tickets() -> list[TicketRow]:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM tickets ORDER BY id").fetchall()
    return [_ticket_row(r) for r in rows]


def load_ticket(row: int) -> TicketRow | None:
    with get_conn() as c:
        r = c.execute("SELECT * FROM tickets WHERE id=?", (row,)).fetchone()
    return _ticket_row(r) if r else None


def ticket_id_for_number(ticket_number: str) -> str | None:
    """Free lookup of Zoho's internal id for a ticket NUMBER, from our own
    table — replaces the old hidden _TicketMeta sheet."""
    with get_conn() as c:
        r = c.execute("SELECT ticket_id FROM tickets WHERE ticket=?", (ticket_number,)).fetchone()
    return r["ticket_id"] if r and r["ticket_id"] else None


def write_main(row: int, field: str, value) -> None:
    if field == "trigger":
        field = "trigger_value"
    with get_conn() as c:
        c.execute(f"UPDATE tickets SET {field}=? WHERE id=?", (value, row))


def save_main_fields(row: int, values: dict) -> list[str]:
    """values: {field name -> new text} for MAIN_EDITABLE fields; only fields
    that actually changed are written. Returns the field names written."""
    current = load_ticket(row)
    if not current:
        return []
    written = []
    with get_conn() as c:
        for field in MAIN_EDITABLE:
            if field in values and values[field] != getattr(current, field):
                col = "trigger_value" if field == "trigger" else field
                c.execute(f"UPDATE tickets SET {col}=? WHERE id=?", (values[field], row))
                written.append(field)
    return written


# ---------------------------------------------------------------------------
# Community Refund
# ---------------------------------------------------------------------------

@dataclass
class RefundRow:
    row: int             # DB id
    timestamp: str
    timestamp_ms: int | None
    name: str
    email: str
    phone: str
    group: str
    reason: str
    funnel: str
    funnel_final: str
    community: str
    amount: str
    trigger: str
    sent_at: str
    sent_by: str
    result: str
    handoff: str

    @property
    def brand(self) -> str:
        return compute_brand(self.community)

    # Kept for template compatibility — these two columns were always
    # reserved/empty slack on the sheet (HANDOVER.txt §3) and carry nothing.
    spare_l: str = ""
    spare_m: str = ""

    @property
    def no_order(self) -> bool:
        return RF_NO_ORDER.lower() in self.funnel_final.lower()

    @property
    def sent(self) -> bool:
        return bool(self.sent_at)

    @property
    def pending(self) -> bool:
        return self.trigger.lower() == "yes" and not self.sent

    @property
    def key(self) -> str:
        """Same identity Apps Script used — epoch ms + '|' + email — so a row
        synced in by the new refund-intake job can't collide with itself."""
        ts = str(self.timestamp_ms) if self.timestamp_ms is not None else self.timestamp
        return f"{ts}|{self.email.lower()}"

    def blockers(self, handoff_on: bool = True) -> list[str]:
        out = []
        if not self.email:
            out.append("email")
        if not self.name:
            out.append("name")
        if not self.no_order:
            if not self.community:
                out.append("community")
            if not self.amount:
                out.append("amount")
            if handoff_on and not self.brand:
                out.append("brand — none derived from the community name")
        return out


REFUND_EDITABLE = {"community", "trigger"}


def _refund_row(r) -> RefundRow:
    ts_iso = r["timestamp_at"]
    ms = None
    if ts_iso:
        try:
            ms = int(datetime.fromisoformat(ts_iso).timestamp() * 1000)
        except ValueError:
            ms = None
    return RefundRow(
        row=r["id"], timestamp=fmt_dt(ts_iso), timestamp_ms=ms,
        name=r["name"], email=r["email"], phone=r["phone"], group=r["group_name"],
        reason=r["reason"], funnel=r["funnel"], funnel_final=r["funnel_final"],
        community=r["community"], amount=r["amount"], trigger=r["trigger_value"],
        sent_at=r["sent_at"], sent_by=r["sent_by"], result=r["result"], handoff=r["handoff"],
    )


def load_refunds() -> list[RefundRow]:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM refunds ORDER BY id").fetchall()
    return [_refund_row(r) for r in rows]


def load_refund(row: int) -> RefundRow | None:
    with get_conn() as c:
        r = c.execute("SELECT * FROM refunds WHERE id=?", (row,)).fetchone()
    return _refund_row(r) if r else None


def write_refund(row: int, field: str, value) -> None:
    if field == "trigger":
        field = "trigger_value"
    elif field == "group":
        field = "group_name"
    with get_conn() as c:
        c.execute(f"UPDATE refunds SET {field}=? WHERE id=?", (value, row))
    _nudge_tracker(f"refund row {row}: {field}")


def save_refund_fields(row: int, values: dict) -> list[str]:
    current = load_refund(row)
    if not current:
        return []
    written = []
    with get_conn() as c:
        for field in REFUND_EDITABLE:
            if field in values and values[field] != getattr(current, field):
                col = "trigger_value" if field == "trigger" else field
                c.execute(f"UPDATE refunds SET {col}=? WHERE id=?", (values[field], row))
                written.append(field)
    if written:
        _nudge_tracker(f"refund row {row}: " + ", ".join(written))
    return written


def _nudge_tracker(why: str) -> None:
    """The Master Refund Tracker mirrors the refunds — tell it something changed
    (tracker_app.nudge is fire-and-forget; imported late to avoid a cycle)."""
    try:
        from . import tracker_app
        tracker_app.nudge(why)
    except Exception:  # noqa: BLE001
        pass


# Header labels for the list pages, unchanged from the sheet layout.
MAIN_HEADERS = [
    ("A", "Ticket"), ("B", "Owner"), ("C", "Ticket date"), ("D", "Entered"), ("E", "Brand"),
    ("F", "Learner"), ("G", "Email"), ("H", "Phone"), ("I", "Course"), ("J", "Requirement"),
    ("K", "Resolution"), ("L", "Resolution status"), ("M", "Trigger"), ("N", "Sent"),
    ("O", "Trigger by"), ("Q", "Category"), ("R", "Reply status"), ("T", "Zoho status"),
]
REFUND_HEADERS = [
    ("A", "Submitted"), ("B", "Name"), ("C", "Email"), ("D", "Phone"), ("E", "Group (form)"),
    ("F", "Reason"), ("G", "Funnel"), ("H", "Funnel final"), ("I", "Group"), ("J", "Amount"),
    ("K", "Brand"), ("L", ""), ("M", ""), ("N", "Trigger"), ("O", "Sent"), ("P", "Trigger by"),
    ("Q", "Reply status"), ("R", "Team handoff"),
]
