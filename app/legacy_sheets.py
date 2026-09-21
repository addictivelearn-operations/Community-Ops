"""
NOT imported by the running app any more — superseded by store.py, which
reads/writes the app's own SQLite database instead of these two sheet tabs.

Kept only so the one-off migration script (reads the live sheets one last
time and copies every row into the new database) has something to read the
old tabs with. Delete this file once that migration has run.
"""

from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from .config import MAIN, RF, settings
from .google import GoogleClient

INTERNAL_DOMAINS = {"lawsikho.com", "lawsikho.in", "skillarbitra.ge",
                    "skillarbitrage.ge", "addictivelearn.com", "ipleaders.in"}

TRIGGER_FALLBACK = ["Yes", "Pending", "NA"]


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def cell(row: list, col: int) -> str:
    """1-based column → trimmed string ('' when absent)."""
    v = row[col - 1] if len(row) >= col else ""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def now_stamp() -> str:
    return datetime.now(settings.tz).strftime("%d/%m/%Y %H:%M:%S")


def serial_to_dt(v) -> datetime | None:
    """A date cell arrives as a Sheets serial (days since 1899-12-30, local
    wall time). Converted exactly, so the epoch matches Apps Script's
    Date.getTime() for the same cell."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    ms = round((v - 25569) * 86400000)
    naive = datetime(1970, 1, 1) + timedelta(milliseconds=ms)
    return naive.replace(tzinfo=settings.tz)


def date_cell(row: list, col: int) -> tuple[str, int | None]:
    """→ (display string, epoch ms or None). Text cells are shown as-is."""
    v = row[col - 1] if len(row) >= col else ""
    d = serial_to_dt(v)
    if d is None:
        return cell(row, col), None
    return d.strftime("%d/%m/%Y %H:%M:%S"), int(d.timestamp() * 1000)


def is_internal(email: str) -> bool:
    return email.rsplit("@", 1)[-1].lower() in INTERNAL_DOMAINS if "@" in email else False


# ---------------------------------------------------------------------------
# Dropdown options, read from the sheet's validation rules (cached per process)
# ---------------------------------------------------------------------------

_options: dict[str, tuple[float, list[str] | None]] = {}
OPTIONS_TTL = 5 * 60   # re-read a dropdown's rule from the sheet every 5 minutes


def clear_options_cache() -> None:
    _options.clear()


def options(g: GoogleClient, sheet_id: str, tab: str, a1: str, fallback=None) -> list[str] | None:
    import time
    key = f"{sheet_id}|{tab}|{a1}"
    hit = _options.get(key)
    if not hit or time.time() - hit[0] > OPTIONS_TTL:
        try:
            got = g.validation_list(sheet_id, tab, a1)
        except Exception:  # noqa: BLE001 — a missing rule is not an error
            got = None
        _options[key] = (time.time(), got)
        hit = _options[key]
    return hit[1] if hit[1] is not None else fallback


def fix_resolution_status(g: GoogleClient, options_list: list[str]) -> str:
    """Sets Column L's dropdown on the main tab to `options_list` for every
    row, and corrects the historical misspelling "Apporoved" in existing
    cells. Same job as fixResolutionStatusColumn() in Apps Script, done from
    here so it needs no paste-and-run."""
    gid = None
    for t in g.sheet_tabs(settings.tracker_sheet_id):
        if t["title"] == settings.main_tab:
            gid = t["sheetId"]
    if gid is None:
        raise RuntimeError(f'Tab "{settings.main_tab}" not found')
    col0 = MAIN.RES_STATUS - 1
    g.sheet_format(settings.tracker_sheet_id, [{
        "setDataValidation": {
            "range": {"sheetId": gid, "startRowIndex": 1,
                      "startColumnIndex": col0, "endColumnIndex": col0 + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": o} for o in options_list]},
                     "strict": False, "showCustomUi": True},
        }
    }])
    letter = col_letter(MAIN.RES_STATUS)
    values = g.sheet_values(settings.tracker_sheet_id, settings.main_tab, f"{letter}2:{letter}")
    fixes = {}
    for i, row in enumerate(values):
        if cell(row, 1).lower() == "apporoved":
            fixes[f"{letter}{i + 2}"] = "Approved"
    g.sheet_write_cells(settings.tracker_sheet_id, settings.main_tab, fixes)
    clear_options_cache()
    return (f'Column L dropdown set to {" / ".join(options_list)} on every row; '
            f'{len(fixes)} "Apporoved" cell(s) changed to "Approved".')


_main_last_row: dict = {}


def main_options(g: GoogleClient, col: int, fallback=None):
    """Reads the rule from the LAST data row: a dropdown applied after the
    early rows were imported is missing on row 400 but present on new rows."""
    if "row" not in _main_last_row:
        try:
            col_a = g.sheet_values(settings.tracker_sheet_id, settings.main_tab,
                                   f"A{settings.main_first_row}:A")
            _main_last_row["row"] = settings.main_first_row + max(
                (i for i, r in enumerate(col_a) if cell(r, 1)), default=0)
        except Exception:  # noqa: BLE001
            _main_last_row["row"] = settings.main_first_row
    for row in (_main_last_row["row"], settings.main_first_row):
        got = options(g, settings.tracker_sheet_id, settings.main_tab, f"{col_letter(col)}{row}")
        if got:
            return got
    return fallback


def refund_options(g: GoogleClient, col: int, fallback=None):
    return options(g, settings.tracker_sheet_id, settings.refund_tab, f"{col_letter(col)}2", fallback)


# ---------------------------------------------------------------------------
# Main tab (ticket replies)
# ---------------------------------------------------------------------------

@dataclass
class TicketRow:
    row: int
    ticket: str          # A
    owner: str           # B
    created: str         # C
    imported_at: str     # D
    brand: str           # E
    name: str            # F
    email: str           # G
    phone: str           # H
    course: str          # I
    requirement: str     # J
    resolution: str      # K
    res_status: str      # L
    trigger: str         # M
    sent_at: str         # N
    sent_by: str         # O
    course_wp: str       # P
    category: str        # Q
    result: str          # R
    thread_link: str     # S
    zoho_status: str     # T

    @property
    def sent(self) -> bool:
        return bool(self.sent_at)

    @property
    def pending(self) -> bool:
        return self.trigger.lower() == "yes" and not self.sent

    @property
    def last_sent_line(self) -> str:
        return self.sent_at.split("\n")[0] if self.sent_at else ""


# Which columns an agent may type into (everything else is written by the
# sync, the categoriser, or the send itself).
MAIN_EDITABLE = {
    "course": MAIN.COURSE, "requirement": MAIN.REQUIREMENT, "resolution": MAIN.RESOLUTION,
    "res_status": MAIN.RES_STATUS, "trigger": MAIN.TRIGGER,
}


def phone_text(v: str) -> str:
    """Rows imported before the RichTextValue fix hold "+91 …" as a formula:
    the API returns "#ERROR!" or the literal "=+91 …". Show the number where
    it can be recovered; run repairPhoneColumn() in Apps Script to fix cells."""
    if v.startswith("="):
        return v[1:].strip()
    if v == "#ERROR!":
        return "(broken cell — run repairPhoneColumn)"
    return v


def _ticket_row(r: list, row: int) -> TicketRow:
    return TicketRow(
        row=row,
        ticket=cell(r, MAIN.TICKET), owner=cell(r, MAIN.OWNER),
        created=date_cell(r, MAIN.TIMESTAMP)[0], imported_at=date_cell(r, MAIN.IMPORTED_AT)[0],
        brand=cell(r, MAIN.BRAND), name=cell(r, MAIN.NAME), email=cell(r, MAIN.EMAIL),
        phone=phone_text(cell(r, MAIN.PHONE)), course=cell(r, MAIN.COURSE), requirement=cell(r, MAIN.REQUIREMENT),
        resolution=cell(r, MAIN.RESOLUTION), res_status=cell(r, MAIN.RES_STATUS),
        trigger=cell(r, MAIN.TRIGGER), sent_at=cell(r, MAIN.SENT_AT), sent_by=cell(r, MAIN.SENT_BY),
        course_wp=cell(r, MAIN.COURSE_WP), category=cell(r, MAIN.CATEGORY),
        result=cell(r, MAIN.RESULT), thread_link=cell(r, MAIN.THREAD_LINK),
        zoho_status=cell(r, MAIN.STATUS),
    )


def load_tickets(g: GoogleClient) -> list[TicketRow]:
    rng = f"A{settings.main_first_row}:{col_letter(MAIN.LAST)}"
    values = g.sheet_values(settings.tracker_sheet_id, settings.main_tab, rng)
    return [_ticket_row(r, settings.main_first_row + i)
            for i, r in enumerate(values) if cell(r, MAIN.TICKET)]


def load_ticket(g: GoogleClient, row: int) -> TicketRow | None:
    values = g.sheet_values(settings.tracker_sheet_id, settings.main_tab,
                            f"A{row}:{col_letter(MAIN.LAST)}{row}")
    if not values or not cell(values[0], MAIN.TICKET):
        return None
    return _ticket_row(values[0], row)


def ticket_meta_ids(g: GoogleClient) -> dict[str, str]:
    """ticketNumber → ticketId from the hidden _TicketMeta sheet (free lookup)."""
    try:
        values = g.sheet_values(settings.tracker_sheet_id, "_TicketMeta", "A2:B")
    except Exception:  # noqa: BLE001 — the sheet may not exist yet
        return {}
    return {cell(r, 2): cell(r, 1) for r in values if cell(r, 2) and cell(r, 1)}


def write_main(g: GoogleClient, row: int, col: int, value) -> None:
    g.sheet_write(settings.tracker_sheet_id, settings.main_tab,
                  f"{col_letter(col)}{row}", [[value]])


def save_main_fields(g: GoogleClient, row: int, values: dict) -> list[str]:
    """values: {field name → new text} for MAIN_EDITABLE fields; only cells
    that actually changed are written. Returns the column letters written."""
    current = load_ticket(g, row)
    if not current:
        return []
    cells = {}
    for field, col in MAIN_EDITABLE.items():
        if field in values and values[field] != getattr(current, field):
            cells[f"{col_letter(col)}{row}"] = values[field]
    g.sheet_write_cells(settings.tracker_sheet_id, settings.main_tab, cells)
    return [a1.rstrip("0123456789") for a1 in cells]


# ---------------------------------------------------------------------------
# Refund tab
# ---------------------------------------------------------------------------

RF_NO_ORDER = "No order found"


@dataclass
class RefundRow:
    row: int
    timestamp: str
    timestamp_ms: int | None
    name: str            # B
    email: str           # C
    phone: str           # D
    group: str           # E
    reason: str          # F
    funnel: str          # G
    funnel_final: str    # H
    community: str       # I
    amount: str          # J
    brand: str           # K (formula)
    spare_l: str         # L
    spare_m: str         # M
    trigger: str         # N
    sent_at: str         # O
    sent_by: str         # P
    result: str          # Q
    handoff: str         # R

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
        """Same identity the Apps Script uses — Date.getTime() + '|' + email for
        a date cell, the raw text otherwise — so a tracker row written by
        either side is found by the other."""
        ts = str(self.timestamp_ms) if self.timestamp_ms is not None else self.timestamp
        return f"{ts}|{self.email.lower()}"

    def blockers(self, handoff_on: bool = True) -> list[str]:
        out = []
        if not self.email:
            out.append("email (C)")
        if not self.name:
            out.append("name (B)")
        if not self.no_order:
            if not self.community:
                out.append("community (I)")
            if not self.amount:
                out.append("amount (J)")
            if handoff_on and not self.brand:
                out.append("brand (K) — the formula found none")
        return out


# A–J arrive from the form and K is a formula. Agents may correct I (the
# cleaned community name, which feeds the learner email AND the brand formula)
# and set N. L and M are reserved for future form columns — HANDOVER §3.
REFUND_EDITABLE = {"community": RF.COMMUNITY, "trigger": RF.TRIGGER}


def save_refund_fields(g: GoogleClient, row: int, values: dict) -> list[str]:
    """values: {field → new text} for REFUND_EDITABLE fields; only changed
    cells are written. Returns the column letters written."""
    current = load_refund(g, row)
    if not current:
        return []
    cells = {}
    for field, col in REFUND_EDITABLE.items():
        if field in values and values[field] != getattr(current, field):
            cells[f"{col_letter(col)}{row}"] = values[field]
    g.sheet_write_cells(settings.tracker_sheet_id, settings.refund_tab, cells)
    return [a1.rstrip("0123456789") for a1 in cells]


def _refund_row(r: list, row: int) -> RefundRow:
    shown, ms = date_cell(r, RF.TIMESTAMP)
    return RefundRow(
        row=row,
        timestamp=shown, timestamp_ms=ms, name=cell(r, RF.NAME), email=cell(r, RF.EMAIL),
        phone=cell(r, RF.PHONE), group=cell(r, RF.GROUP), reason=cell(r, RF.REASON),
        funnel=cell(r, RF.FUNNEL), funnel_final=cell(r, RF.FUNNEL_FINAL),
        community=cell(r, RF.COMMUNITY), amount=cell(r, RF.AMOUNT), brand=cell(r, RF.BRAND),
        spare_l=cell(r, 12), spare_m=cell(r, 13),
        trigger=cell(r, RF.TRIGGER), sent_at=cell(r, RF.SENT_AT), sent_by=cell(r, RF.SENT_BY),
        result=cell(r, RF.RESULT), handoff=cell(r, RF.HANDOFF),
    )


def load_refunds(g: GoogleClient) -> list[RefundRow]:
    values = g.sheet_values(settings.tracker_sheet_id, settings.refund_tab,
                            f"A2:{col_letter(RF.LAST)}")
    return [_refund_row(r, i + 2) for i, r in enumerate(values) if cell(r, RF.TIMESTAMP)]


def load_refund(g: GoogleClient, row: int) -> RefundRow | None:
    values = g.sheet_values(settings.tracker_sheet_id, settings.refund_tab,
                            f"A{row}:{col_letter(RF.LAST)}{row}")
    if not values or not cell(values[0], RF.TIMESTAMP):
        return None
    return _refund_row(values[0], row)


def write_refund(g: GoogleClient, row: int, col: int, value) -> None:
    g.sheet_write(settings.tracker_sheet_id, settings.refund_tab,
                  f"{col_letter(col)}{row}", [[value]])


# Header labels for the list pages, in sheet order.
MAIN_HEADERS = [
    ("A", "Ticket"), ("B", "Owner"), ("C", "Ticket date"), ("D", "Entered"), ("E", "Brand"),
    ("F", "Learner"), ("G", "Email"), ("H", "Phone"), ("I", "Course"), ("J", "Requirement"),
    ("K", "Resolution"), ("L", "Resolution status"), ("M", "Trigger"), ("N", "Sent"),
    ("O", "Trigger by"), ("Q", "Category"), ("R", "Reply status"), ("T", "Zoho status"),
    # Left out on purpose (the sheet columns are untouched):
    #   P "Course Name (Wordpress)" — only used for the first ~399 rows.
    #   S thread link — points at the Apps Script viewer; the app's own
    #     /tickets/<n> page shows the same conversation from the same calls.
]
REFUND_HEADERS = [
    ("A", "Submitted"), ("B", "Name"), ("C", "Email"), ("D", "Phone"), ("E", "Group (form)"),
    ("F", "Reason"), ("G", "Funnel"), ("H", "Funnel final"), ("I", "Group"), ("J", "Amount"),
    ("K", "Brand"), ("L", ""), ("M", ""), ("N", "Trigger"), ("O", "Sent"), ("P", "Trigger by"),
    ("Q", "Reply status"), ("R", "Team handoff"),
]
