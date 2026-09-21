"""
One-off: copy every row out of the two sheets (the ticket tracker from row
400, the Community Refund tab from row 2) into the app's own database
(db.py), preserving every workflow-state field exactly — trigger, sent_at,
sent_by, result, category, handoff — so nothing gets double-sent once the
app starts driving from the database instead of the sheet.

Safe to re-run: tickets are matched on their (unique) ticket number and
refunds on the same (timestamp, email) identity Apps Script itself uses for
dedupe, so an already-migrated row is skipped, not duplicated.

Uses legacy_sheets.py (not imported anywhere else) since that is the one
module left that still knows how to read the two tabs from the Sheets API.
"""

from dataclasses import dataclass
from datetime import datetime

from . import legacy_sheets as sheets
from .config import settings
from .db import get_conn, set_state
from .google import GoogleClient
from .store import load_refunds as load_db_refunds

REFUNDS_MIGRATED_KEY = "REFUNDS_MIGRATED"


@dataclass
class MigrationReport:
    tickets_migrated: int = 0
    tickets_skipped: int = 0
    tickets_failed: list[str] = None
    refunds_migrated: int = 0
    refunds_skipped: int = 0
    refunds_failed: list[str] = None

    def __post_init__(self):
        self.tickets_failed = self.tickets_failed or []
        self.refunds_failed = self.refunds_failed or []


def _parse_display(s: str) -> str:
    """"%d/%m/%Y %H:%M:%S" display string (as sheets.date_cell produced) →
    ISO, or '' if blank/unparseable."""
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%d/%m/%Y %H:%M:%S").replace(tzinfo=settings.tz).isoformat()
    except ValueError:
        return ""


def _iso_from_ms(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=settings.tz).isoformat()


def run_migration(g: GoogleClient) -> MigrationReport:
    report = MigrationReport()

    tickets = sheets.load_tickets(g)
    with get_conn() as c:
        for t in tickets:
            if not t.ticket:
                continue
            try:
                cur = c.execute(
                    """INSERT OR IGNORE INTO tickets
                       (ticket, owner, created_at, imported_at, brand, name, email, phone,
                        course, requirement, resolution, res_status, trigger_value, sent_at,
                        sent_by, category, result, zoho_status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t.ticket, t.owner, _parse_display(t.created), _parse_display(t.imported_at),
                     t.brand, t.name, t.email, t.phone, t.course, t.requirement, t.resolution,
                     t.res_status, t.trigger, t.sent_at, t.sent_by, t.category, t.result,
                     t.zoho_status))
                if cur.rowcount:
                    report.tickets_migrated += 1
                else:
                    report.tickets_skipped += 1
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the batch
                report.tickets_failed.append(f"#{t.ticket}: {e}")

    refunds = sheets.load_refunds(g)
    # Same identity Apps Script itself uses for dedupe (RefundRow.key,
    # timestamp-ms + lowercased email) — reused via the DB-backed dataclass
    # so both sides can never disagree about what counts as "the same row".
    existing_keys = {existing.key for existing in load_db_refunds()}
    with get_conn() as c:
        for r in refunds:
            if r.key in existing_keys:
                report.refunds_skipped += 1
                continue
            try:
                c.execute(
                    """INSERT INTO refunds
                       (timestamp_at, name, email, phone, group_name, reason, funnel,
                        funnel_final, community, amount, trigger_value, sent_at, sent_by,
                        result, handoff)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_iso_from_ms(r.timestamp_ms) or r.timestamp, r.name, r.email, r.phone,
                     r.group, r.reason, r.funnel, r.funnel_final, r.community, r.amount,
                     r.trigger, r.sent_at, r.sent_by, r.result, r.handoff))
                existing_keys.add(r.key)
                report.refunds_migrated += 1
            except Exception as e:  # noqa: BLE001
                report.refunds_failed.append(f"{r.email} @ {r.timestamp}: {e}")

    # Marks that the historical refund rows have been carried over, so
    # refund_intake.run() (manual or scheduled) knows it's safe to start
    # inserting fresh Refund_Clean rows without orphaning an already-sent
    # row's workflow state — see refund_intake.py's guard.
    set_state(REFUNDS_MIGRATED_KEY, "1")

    return report
