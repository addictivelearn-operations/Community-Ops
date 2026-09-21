"""
Course Master sheet — a local mirror of an external, read-only purchase/
enrolment sheet, refreshed on a schedule. Port of getCourseSheetIndex_
(06_LearnerCache.gs), except the .gs version re-read and re-indexed the whole
external sheet on every single ticket; that doesn't scale to a request-driven
pipeline, so this ETLs it into `course_purchases` instead and every lookup is
an indexed SQL query.

Course names come ONLY from this sheet (email match first, then phone) —
never from Revenue. Read via the unattended service-account GoogleClient
(see config.sync_service_account_email) since this runs on a timer with
nobody signed in.
"""

from .config import settings
from .db import get_conn
from .google import GoogleClient
from .legacy_sheets import col_letter


def phone_key(raw: str) -> str:
    """Last 10 digits, so '1000000000', '911000000000', '+91 10000 00000'
    all produce the same key. '' when fewer than 10 digits."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def refresh() -> dict:
    """Re-reads the whole Course Master sheet and replaces course_purchases.
    A later row for the same email/phone in the sheet wins — matching the
    .gs 'most recent purchase wins' rule — since rows are inserted in sheet
    order and lookups take the LAST match (see lookup_by_*)."""
    if not settings.course_sheet_enabled or not settings.course_sheet_id:
        return {"rows": 0, "skipped": "COURSE_SHEET_ENABLED is off or no sheet id"}

    g = GoogleClient(settings.sync_service_account_email)
    tab = settings.course_sheet_tab or g.sheet_tabs(settings.course_sheet_id)[0]["title"]

    # Find the sheet's real extent from the email column (cheap, avoids
    # reading a huge empty tail).
    email_letter = col_letter(settings.course_sheet_email_col)
    email_col_probe = g.sheet_values(settings.course_sheet_id, tab, f"{email_letter}2:{email_letter}")
    last_row = 1 + len(email_col_probe)
    if last_row < 2:
        return {"rows": 0}

    def col(n: int) -> list[str]:
        letter = col_letter(n)
        values = g.sheet_values(settings.course_sheet_id, tab, f"{letter}2:{letter}{last_row}")
        return [str(v[0]).strip() if v else "" for v in values]

    names = col(settings.course_sheet_name_col)
    emails = col(settings.course_sheet_email_col)
    phones = col(settings.course_sheet_phone_col)
    courses = col(settings.course_sheet_course_col)

    n = last_row - 1
    with get_conn() as c:
        c.execute("DELETE FROM course_purchases")
        for i in range(n):
            email = (emails[i] if i < len(emails) else "").strip().lower()
            phone_raw = (phones[i] if i < len(phones) else "").strip()
            course = (courses[i] if i < len(courses) else "").strip()
            name = (names[i] if i < len(names) else "").strip()
            if not (email and "@" in email) and not phone_key(phone_raw):
                continue
            c.execute("INSERT INTO course_purchases (email, phone_key, phone, name, course) VALUES (?,?,?,?,?)",
                     (email, phone_key(phone_raw), phone_raw, name, course))
    return {"rows": n}


def lookup_by_email(email: str) -> dict | None:
    """{'name', 'phone', 'course'} for a learner email — the LAST-indexed
    (most recent) row wins, matching the .gs 'later rows overwrite' rule."""
    if not email:
        return None
    with get_conn() as c:
        r = c.execute(
            "SELECT name, phone, course FROM course_purchases WHERE email=? ORDER BY id DESC LIMIT 1",
            (email.strip().lower(),)).fetchone()
    return {"name": r["name"], "phone": r["phone"], "course": r["course"]} if r else None


def lookup_by_phone(phone: str) -> dict | None:
    key = phone_key(phone)
    if not key:
        return None
    with get_conn() as c:
        r = c.execute(
            "SELECT name, phone, course FROM course_purchases WHERE phone_key=? ORDER BY id DESC LIMIT 1",
            (key,)).fetchone()
    return {"name": r["name"], "phone": r["phone"], "course": r["course"]} if r else None
