"""
Mirrors new rows from the Google Form's `Refund_Clean` landing sheet into the
app's own `refunds` table — port of RF_01_Sync.gs's syncRefundRows().

Refund_Clean itself stays a sheet (Forms can only submit into a Sheet); this
is the one job that still reads a Google Sheet on a schedule, via the
unattended service-account GoogleClient (config.sync_service_account_email),
since nobody is signed in when the timer fires.

Only columns A-J are read (mirrors the form exactly); Brand is derived at
read time from the community name (store.compute_brand), and the workflow
columns (trigger/sent_at/sent_by/result/handoff) start blank for every new
row, same as the .gs source.
"""

from datetime import datetime

from .config import settings
from .db import get_conn, get_state
from .google import GoogleClient
from .legacy_sheets import cell, col_letter, serial_to_dt
from .migrate import REFUNDS_MIGRATED_KEY
from .store import load_refunds

SOURCE_LAST_COL = 10  # A..J


def _row_key(timestamp_ms: int | None, timestamp_text: str, email: str) -> str:
    ts = str(timestamp_ms) if timestamp_ms is not None else timestamp_text
    return f"{ts}|{email.strip().lower()}"


def run() -> dict:
    """Reads Refund_Clean and inserts rows not already in `refunds`, on or
    after settings.rf_since_iso. Safe to run on any schedule — only appends
    what it has not seen (same identity RefundRow.key already uses).

    Refuses to run until the one-off sheet migration has happened
    (migrate.py sets REFUNDS_MIGRATED_KEY): running this first would insert
    fresh rows with blank trigger/sent state for refunds that were already
    approved from the sheet, which could get a learner emailed twice once
    the migration runs afterward and sees the key already "taken"."""
    if not get_state(REFUNDS_MIGRATED_KEY):
        return {"skipped": "historical refunds not migrated yet — "
                          "run 'Migrate sheet rows into the database' on /diagnostics first"}

    g = GoogleClient(settings.sync_service_account_email)
    values = g.sheet_values(settings.rf_source_sheet_id, settings.rf_source_tab,
                            f"A2:{col_letter(SOURCE_LAST_COL)}")
    if not values:
        return {"inserted": 0, "skipped_old": 0}

    cutoff = datetime.fromisoformat(settings.rf_since_iso)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=settings.tz)

    existing_keys = {r.key for r in load_refunds()}

    inserted = 0
    skipped_old = 0
    with get_conn() as c:
        for row in values:
            ts_raw = row[0] if len(row) > 0 else ""
            if not ts_raw:
                continue
            ts_dt = serial_to_dt(ts_raw)
            if ts_dt is None:
                try:
                    ts_dt = datetime.fromisoformat(str(ts_raw)).replace(tzinfo=settings.tz)
                except ValueError:
                    continue
            if ts_dt < cutoff:
                skipped_old += 1
                continue

            email = cell(row, 3)
            ts_ms = int(ts_dt.timestamp() * 1000)
            key = _row_key(ts_ms, ts_dt.isoformat(), email)
            if key in existing_keys:
                continue
            existing_keys.add(key)  # guards duplicates within this same batch too

            c.execute(
                """INSERT INTO refunds
                   (timestamp_at, name, email, phone, group_name, reason, funnel,
                    funnel_final, community, amount)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ts_dt.isoformat(), cell(row, 2), email, cell(row, 4), cell(row, 5),
                 cell(row, 6), cell(row, 7), cell(row, 8), cell(row, 9), cell(row, 10)))
            inserted += 1

    if inserted:
        try:
            from . import tracker_app
            tracker_app.nudge(f"{inserted} new refund row(s) from Refund_Clean")
        except Exception:  # noqa: BLE001
            pass
    return {"inserted": inserted, "skipped_old": skipped_old}
