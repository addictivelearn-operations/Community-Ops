"""
The app's own SQLite database — replaces the Sheets API as the backing store
for the two tabs the app used to read/write directly (the ticket tracker and
the Community Refund tab). Everything else (the finance team's tracker,
Refund_Clean, Drive, Gmail) still goes through google.py exactly as before;
this file only owns what used to live in those two tabs.

Plain stdlib sqlite3, matching the existing data/tokens.db pattern — no ORM,
no new dependency. WAL mode so a background sync job and a web request don't
lock each other out.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ops.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket        TEXT NOT NULL UNIQUE,   -- Zoho ticket NUMBER (was Column A)
    ticket_id     TEXT NOT NULL DEFAULT '',  -- Zoho's internal id
    owner         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',  -- ISO — ticket created (Zoho)
    imported_at   TEXT NOT NULL DEFAULT '',  -- ISO — when the row was synced
    brand         TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    course        TEXT NOT NULL DEFAULT '',
    requirement   TEXT NOT NULL DEFAULT '',
    resolution    TEXT NOT NULL DEFAULT '',
    res_status    TEXT NOT NULL DEFAULT '',
    trigger_value TEXT NOT NULL DEFAULT '',
    sent_at       TEXT NOT NULL DEFAULT '',  -- stacked stamps, newest first
    sent_by       TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    zoho_status   TEXT NOT NULL DEFAULT '',
    modified_time TEXT NOT NULL DEFAULT '',  -- Zoho's modifiedTime, for the AI cache
    extraction_pending INTEGER NOT NULL DEFAULT 0  -- 1 = name/phone/course still owed (Gemini failed at insert)
);

CREATE TABLE IF NOT EXISTS refunds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_at  TEXT NOT NULL DEFAULT '',  -- ISO — form submission time
    name          TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    group_name    TEXT NOT NULL DEFAULT '',  -- raw form "Group" (Column E)
    reason        TEXT NOT NULL DEFAULT '',
    funnel        TEXT NOT NULL DEFAULT '',
    funnel_final  TEXT NOT NULL DEFAULT '',
    community     TEXT NOT NULL DEFAULT '',  -- cleaned community name (Column I) — Brand is derived from this
    amount        TEXT NOT NULL DEFAULT '',
    trigger_value TEXT NOT NULL DEFAULT '',
    sent_at       TEXT NOT NULL DEFAULT '',
    sent_by       TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    handoff       TEXT NOT NULL DEFAULT ''
);

-- Port of the _LearnerCache hidden sheet (06_LearnerCache.gs): fills gaps for
-- a learner who raises another ticket later, merging non-empty values so a
-- ticket that only reveals the phone doesn't erase a known course.
CREATE TABLE IF NOT EXISTS learner_cache (
    email      TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    course     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''  -- ISO
);

-- Port of _AIExtractionCache: a ticket is never sent to Claude twice unless
-- it materially changed (Zoho modifiedTime differs) or the entry expired.
CREATE TABLE IF NOT EXISTS ai_extraction_cache (
    ticket_id      TEXT PRIMARY KEY,
    modified_time  TEXT NOT NULL DEFAULT '',
    extraction_json TEXT NOT NULL DEFAULT '{}',
    cached_at      TEXT NOT NULL DEFAULT ''  -- ISO
);

-- Local mirror of the external Course Master sheet, refreshed on a schedule
-- instead of being re-read in full on every ticket (06_LearnerCache.gs's
-- getCourseSheetIndex_ re-read the whole sheet every execution — fine in
-- Apps Script, too slow for a request-driven port).
CREATE TABLE IF NOT EXISTS course_purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL DEFAULT '',
    phone_key   TEXT NOT NULL DEFAULT '',  -- last 10 digits, for phone lookups
    phone       TEXT NOT NULL DEFAULT '',  -- as it appears in the sheet
    name        TEXT NOT NULL DEFAULT '',
    course      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_course_purchases_email ON course_purchases(email);
CREATE INDEX IF NOT EXISTS idx_course_purchases_phone ON course_purchases(phone_key);

-- Scalar cursors — replaces PropertiesService (LAST_SYNC_ISO,
-- STATUS_LAST_SWEEP_ISO, the refund-intake watermark).
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Port of the _SyncLog hidden sheet — one row per processed ticket per run.
CREATE TABLE IF NOT EXISTS sync_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL DEFAULT '',  -- ISO
    ticket        TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',  -- OK | ERROR
    ai_used       INTEGER NOT NULL DEFAULT 0,
    revenue_used  INTEGER NOT NULL DEFAULT 0,
    confidence    TEXT NOT NULL DEFAULT '',
    ms            INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT ''
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Columns added to a table AFTER it first shipped. CREATE TABLE IF NOT EXISTS
# is a no-op on a table that already exists, so a column added here later
# needs an explicit ALTER TABLE — add one line per (table, column, ddl) the
# day that happens, rather than editing SCHEMA and assuming it will apply.
_MIGRATIONS = [
    ("course_purchases", "phone", "ALTER TABLE course_purchases ADD COLUMN phone TEXT NOT NULL DEFAULT ''"),
    ("tickets", "extraction_pending", "ALTER TABLE tickets ADD COLUMN extraction_pending INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if cols and column not in cols:
            conn.execute(ddl)


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_state(key: str, default: str = "") -> str:
    with get_conn() as c:
        r = c.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_state(key: str, value: str) -> None:
    with get_conn() as c:
        c.execute("INSERT INTO sync_state (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
