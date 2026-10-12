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

Since 12 Oct 2026 run() also keeps every NOT-YET-EMAILED row in step with the
sheet (mirror_unsent): an edited value is updated, and a row the sheet no
longer has is removed. A row that has been emailed is never touched.
"""

from datetime import datetime

from .config import settings
from .db import get_conn, get_state
from .google import GoogleClient
from .legacy_sheets import cell, col_letter, serial_to_dt
from .migrate import REFUNDS_MIGRATED_KEY
from .store import delete_refund, load_refunds

SOURCE_LAST_COL = 10  # A..J

_FIELDS = ("name", "phone", "group", "reason", "funnel", "funnel_final", "community", "amount")


def _row_key(timestamp_ms: int | None, timestamp_text: str, email: str) -> str:
    ts = str(timestamp_ms) if timestamp_ms is not None else timestamp_text
    return f"{ts}|{email.strip().lower()}"


def _sheet_datetime(row: list) -> datetime | None:
    ts_raw = row[0] if len(row) > 0 else ""
    if not ts_raw:
        return None
    ts_dt = serial_to_dt(ts_raw)
    if ts_dt is None:
        try:
            ts_dt = datetime.fromisoformat(str(ts_raw)).replace(tzinfo=settings.tz)
        except ValueError:
            return None
    return ts_dt


def _read_sheet() -> list[list]:
    g = GoogleClient(settings.sync_service_account_email)
    return g.sheet_values(settings.rf_source_sheet_id, settings.rf_source_tab,
                          f"A2:{col_letter(SOURCE_LAST_COL)}") or []


def _index(values: list[list]) -> dict[str, list]:
    """Source Key -> sheet row, for every row with a readable timestamp."""
    by_key: dict[str, list] = {}
    for row in values:
        ts_dt = _sheet_datetime(row)
        if ts_dt is None:
            continue
        by_key[_row_key(int(ts_dt.timestamp() * 1000), ts_dt.isoformat(), cell(row, 3))] = row
    return by_key


def _cutoff() -> datetime:
    cutoff = datetime.fromisoformat(settings.rf_since_iso)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=settings.tz)
    return cutoff


def _audit(action: str, target: str, detail: str) -> None:
    with get_conn() as c:
        c.execute("INSERT INTO audit_log (at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                  (datetime.now(settings.tz).isoformat(), "system: refund intake", action, target, detail))


def run() -> dict:
    """Reads Refund_Clean and inserts rows not already in `refunds`, on or
    after settings.rf_since_iso, then mirrors edits/removals onto the rows
    that haven't been emailed yet. Safe to run on any schedule — only
    appends what it has not seen (same identity RefundRow.key already uses).

    Refuses to run until the one-off sheet migration has happened
    (migrate.py sets REFUNDS_MIGRATED_KEY): running this first would insert
    fresh rows with blank trigger/sent state for refunds that were already
    approved from the sheet, which could get a learner emailed twice once
    the migration runs afterward and sees the key already "taken"."""
    if not get_state(REFUNDS_MIGRATED_KEY):
        return {"skipped": "historical refunds not migrated yet — "
                          "run 'Migrate sheet rows into the database' on /diagnostics first"}

    values = _read_sheet()
    if not values:
        return {"inserted": 0, "skipped_old": 0}

    cutoff = _cutoff()
    existing_keys = {r.key for r in load_refunds()}

    inserted = 0
    skipped_old = 0
    with get_conn() as c:
        for row in values:
            ts_dt = _sheet_datetime(row)
            if ts_dt is None:
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

    mirrored = mirror_unsent(_index(values), prune=True, cutoff=cutoff)

    if inserted or mirrored.get("updated") or mirrored.get("removed"):
        try:
            from . import tracker_app
            tracker_app.nudge(f"refund intake: {inserted} new, {mirrored.get('updated', 0)} updated, "
                              f"{mirrored.get('removed', 0)} removed")
        except Exception:  # noqa: BLE001
            pass
    return {"inserted": inserted, "skipped_old": skipped_old,
            "updated": mirrored.get("updated", 0), "removed": mirrored.get("removed", 0),
            **({"removal_blocked": mirrored["removal_blocked"]} if mirrored.get("removal_blocked") else {})}


def mirror_unsent(by_key: dict[str, list], prune: bool, cutoff: datetime | None = None) -> dict:
    """Brings every row that has NOT been emailed yet in step with the sheet,
    matched by Source Key (timestamp + email) so a row is only ever touched
    by the exact form submission it came from:
      - a value edited in the sheet (an amount corrected, say) is updated;
      - with prune=True, a row the sheet no longer has is removed (an email
        or timestamp corrected in the sheet makes the sheet row look new, so
        intake inserts it and the old copy would otherwise sit there as a
        duplicate).
    A row once emailed is never touched, no matter what: that is the record
    of what was actually sent.

    Removal safeguards (a bad or partial sheet read must never wipe rows):
    nothing is pruned if the sheet read came back empty, only rows inside
    intake's own window (on/after rf_since_iso) are candidates, and if one
    run would remove more than settings.rf_mirror_max_removals rows it
    removes none and reports removal_blocked instead. Every update and
    removal is written to the audit log."""
    updated = unchanged = not_in_sheet = skipped_sent = 0
    if not by_key:
        return {"updated": 0, "unchanged": 0, "not_in_sheet": 0, "skipped_sent": 0, "removed": 0,
                "note": "sheet read was empty — nothing changed"}

    cutoff_ms = int(cutoff.timestamp() * 1000) if cutoff else None
    stale = []
    changes: list[tuple] = []
    for r in load_refunds():
        if r.sent:
            skipped_sent += 1
            continue
        row = by_key.get(r.key)
        if not row:
            not_in_sheet += 1
            if cutoff_ms is not None and r.timestamp_ms is not None and r.timestamp_ms >= cutoff_ms:
                stale.append(r)
            continue
        new = (cell(row, 2), cell(row, 4), cell(row, 5), cell(row, 6),
               cell(row, 7), cell(row, 8), cell(row, 9), cell(row, 10))
        old = (r.name, r.phone, r.group, r.reason, r.funnel, r.funnel_final, r.community, r.amount)
        if new == old:
            unchanged += 1
            continue
        changes.append((r, new, old))

    with get_conn() as c:
        for r, new, _old in changes:
            c.execute(
                "UPDATE refunds SET name=?, phone=?, group_name=?, reason=?, funnel=?, "
                "funnel_final=?, community=?, amount=? WHERE id=?",
                (*new, r.row))
            updated += 1
    for r, new, old in changes:
        diff = "; ".join(f"{f}: {o!r} -> {n!r}" for f, o, n in zip(_FIELDS, old, new) if o != n)
        _audit("refund_mirror_update", f"row {r.row}", f"{r.name} <{r.email}> — {diff}")

    removed = 0
    blocked = 0
    if prune and stale:
        if len(stale) > settings.rf_mirror_max_removals:
            blocked = len(stale)
            _audit("refund_mirror_blocked", f"{blocked} row(s)",
                   f"more than {settings.rf_mirror_max_removals} unsent row(s) are gone from Refund_Clean "
                   "— none removed; review them under Diagnostics → Not in Refund_Clean")
        else:
            for r in stale:
                if delete_refund(r.row):
                    _audit("refund_mirror_remove", f"row {r.row}",
                           f"{r.name} <{r.email}> {r.community} {r.amount} — no longer in Refund_Clean")
                    removed += 1

    return {"updated": updated, "unchanged": unchanged, "not_in_sheet": not_in_sheet,
            "skipped_sent": skipped_sent, "removed": removed, "removal_blocked": blocked}


def sheet_keys() -> set[str]:
    """Every Source Key currently in Refund_Clean, right now — same identity
    RefundRow.key already uses (timestamp ms + lowercased email). Lets a
    caller ask "does this database row still have a matching form
    submission?" (the Diagnostics "Not in Refund_Clean" view)."""
    return set(_index(_read_sheet()))


def resync_unsent() -> dict:
    """Manual button: re-pulls columns A-J from Refund_Clean for every row
    that has NOT been emailed yet (Kawal, 23 Sep 2026: a Refund_Clean bug put
    the course name in the Reason column for some rows). Updates only — it
    never removes a row (the automatic intake does that, with safeguards).
    A row once emailed is never touched."""
    out = mirror_unsent(_index(_read_sheet()), prune=False)
    return {k: out[k] for k in ("updated", "unchanged", "not_in_sheet", "skipped_sent") if k in out}
