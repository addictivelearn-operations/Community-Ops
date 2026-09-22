"""
Ticket sync — port of 01_Main.gs + the discovery/detail parts of
03_ZohoApi.gs. Pulls Community Team tickets from Zoho Desk that aren't in
the database yet, extracts learner details (deterministic first, Revenue
System and the Course Master sheet next, Claude last for genuine gaps), and
inserts one row per ticket into `tickets`.

Also does the incremental status/owner refresh — port of
refreshRecentTicketStatuses() (12_TicketLinks.gs).

NOT ported: image/OCR attachment handling — see extraction.py's docstring.
"""

import json
import threading
import time
from datetime import datetime, timedelta

from . import categorize, course_master, extraction, revenue, zoho
from .config import settings
from .db import get_conn, get_state, set_state

_lock = threading.Lock()

LAST_SYNC_KEY = "LAST_SYNC_ISO"
STATUS_SYNC_KEY = "STATUS_LAST_SWEEP_ISO"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Learner cache (port of _LearnerCache)
# ---------------------------------------------------------------------------

def _lookup_learner_cache(email: str) -> dict | None:
    with get_conn() as c:
        r = c.execute("SELECT name, phone, course, updated_at FROM learner_cache WHERE email=?",
                     (email.lower(),)).fetchone()
    if not r:
        return None
    updated = _parse_iso(r["updated_at"]) if r["updated_at"] else None
    if updated and (datetime.now(updated.tzinfo) - updated) > timedelta(days=settings.learner_cache_ttl_days):
        return None
    return {"name": r["name"], "phone": r["phone"], "course": r["course"]}


def _upsert_learner_cache(email: str, name: str, phone: str, course: str) -> None:
    if not email or not (name or phone or course):
        return
    now = datetime.now(settings.tz).isoformat()
    with get_conn() as c:
        row = c.execute("SELECT name, phone, course FROM learner_cache WHERE email=?", (email.lower(),)).fetchone()
        if row:
            c.execute("UPDATE learner_cache SET name=?, phone=?, course=?, updated_at=? WHERE email=?",
                     (name or row["name"], phone or row["phone"], course or row["course"], now, email.lower()))
        else:
            c.execute("INSERT INTO learner_cache (email, name, phone, course, updated_at) VALUES (?,?,?,?,?)",
                     (email.lower(), name, phone, course, now))


def _lookup_from_tickets(email: str) -> dict | None:
    """Rows already in the database — port of lookupLearnerFromSheet_.
    Later (more recent) tickets override earlier ones, merging non-empty
    values, same as the .gs source."""
    with get_conn() as c:
        rows = c.execute("SELECT name, phone, course FROM tickets WHERE LOWER(email)=? ORDER BY id",
                        (email.lower(),)).fetchall()
    if not rows:
        return None
    name = phone = course = ""
    for r in rows:
        name = r["name"] or name
        phone = r["phone"] or phone
        course = r["course"] or course
    return {"name": name, "phone": phone, "course": course} if (name or phone or course) else None


# ---------------------------------------------------------------------------
# AI extraction cache (port of _AIExtractionCache)
# ---------------------------------------------------------------------------

def _lookup_ai_cache(ticket_id: str, modified_time: str) -> dict | None:
    with get_conn() as c:
        r = c.execute("SELECT modified_time, extraction_json, cached_at FROM ai_extraction_cache WHERE ticket_id=?",
                     (str(ticket_id),)).fetchone()
    if not r or str(r["modified_time"]) != str(modified_time):
        return None
    cached_at = _parse_iso(r["cached_at"]) if r["cached_at"] else None
    if cached_at and (datetime.now(cached_at.tzinfo) - cached_at) > timedelta(days=settings.ai_cache_ttl_days):
        return None
    try:
        return json.loads(r["extraction_json"])
    except ValueError:
        return None


def _upsert_ai_cache(ticket_id: str, modified_time: str, extraction_result: dict) -> None:
    now = datetime.now(settings.tz).isoformat()
    with get_conn() as c:
        c.execute(
            "INSERT INTO ai_extraction_cache (ticket_id, modified_time, extraction_json, cached_at) "
            "VALUES (?,?,?,?) ON CONFLICT(ticket_id) DO UPDATE SET "
            "modified_time=excluded.modified_time, extraction_json=excluded.extraction_json, cached_at=excluded.cached_at",
            (str(ticket_id), str(modified_time), json.dumps(extraction_result), now))


# ---------------------------------------------------------------------------
# Ticket discovery (port of fetchCommunityTickets_)
# ---------------------------------------------------------------------------

def _existing_ticket_numbers() -> set[str]:
    with get_conn() as c:
        rows = c.execute("SELECT ticket FROM tickets").fetchall()
    return {r["ticket"] for r in rows}


def fetch_community_tickets(existing_ids: set[str]) -> list[dict]:
    dept_map = zoho.department_map()
    last_sync = get_state(LAST_SYNC_KEY, settings.sync_since_iso)
    cutoff = _parse_iso(last_sync) - timedelta(minutes=settings.sync_overlap_minutes)
    team_name_lc = settings.team_name.lower()
    results = []

    for page in range(settings.max_list_pages):
        # sortBy=-createdTime, NOT the default -modifiedTime (22 Sep 2026):
        # modifiedTime turned out to be null for essentially every ticket in
        # this org, not just "some" as first thought on 21 Sep — so a
        # "-modifiedTime" sort returns tickets in an order that has nothing
        # to do with recency (verified live: page 0 spanned 24 Aug-20 Sep,
        # with same-day tickets from minutes earlier never appearing at all).
        # Since the loop below stops paging as soon as it sees one ticket
        # older than cutoff, an unreliable sort meant it could stop before
        # ever reaching a genuinely new ticket — and because run() advances
        # LAST_SYNC_ISO even when 0 candidates are found, a ticket missed
        # this way was never looked at again. createdTime, unlike
        # modifiedTime, is always present and Zoho does sort by it correctly
        # (verified live: a `-createdTime` page came back strictly
        # descending, second-by-second).
        tickets = zoho.tickets_page(from_=page * 100, limit=100, sort="-createdTime")
        if not tickets:
            break
        reached_cutoff = False
        for t in tickets:
            created_time = t.get("createdTime")
            modified_time = t.get("modifiedTime") or created_time
            if _parse_iso(created_time) < cutoff:
                reached_cutoff = True
                continue
            team_name = ((t.get("team") or {}).get("name") or "").lower()
            assignee = t.get("assignee") or {}
            assignee_name = f"{assignee.get('firstName', '')} {assignee.get('lastName', '')}".strip().lower()
            if team_name != team_name_lc and assignee_name != team_name_lc:
                continue
            if settings.allowed_departments:
                dept_name_lc = dept_map.get(str(t.get("departmentId", "")), "").lower()
                if not any(str(d).lower() == dept_name_lc for d in settings.allowed_departments):
                    continue
            if str(t["ticketNumber"]) in existing_ids:
                continue
            results.append({
                "id": t["id"], "ticketNumber": t["ticketNumber"], "email": t.get("email", ""),
                "departmentName": dept_map.get(str(t.get("departmentId", "")), ""),
                "modifiedTime": modified_time,
            })
        if reached_cutoff or len(tickets) < 100:
            break

    results.sort(key=lambda t: t["modifiedTime"])
    seen: set = set()
    out = []
    for t in results:
        if t["ticketNumber"] in seen:
            continue
        seen.add(t["ticketNumber"])
        out.append(t)
    return out


def _fetch_ticket_details(ticket_id: str) -> dict:
    t = zoho.ticket_full(ticket_id) or {}
    contact = t.get("contact") or {}
    return {
        "subject": t.get("subject", ""),
        "description_text": zoho.html_to_text(t.get("description", "")),
        "email": t.get("email") or contact.get("email") or "",
        "phone": t.get("phone") or contact.get("phone") or contact.get("mobile") or "",
        "contact_name": f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
        "created_time": t.get("createdTime", ""),
        "status": t.get("status", ""),
        "owner_name": zoho.owner_label(t),
    }


# ---------------------------------------------------------------------------
# Per-ticket pipeline (port of processTicket_)
# ---------------------------------------------------------------------------

def process_ticket(ticket: dict) -> dict:
    stats = {"ai_used": False, "revenue_used": False, "confidence": ""}

    details = _fetch_ticket_details(ticket["id"])
    conversations = zoho.conversations(ticket["id"])

    det = extraction.extract_deterministic(details, conversations)
    learner_email = (det.email or details["email"] or ticket.get("email") or "").strip().lower()
    name, phone, course = det.name, det.phone, det.course
    course_from_sheet = False

    email_is_external = bool(learner_email) and not extraction.is_internal_email(learner_email)

    if email_is_external and (not name or not phone or not course):
        cached = _lookup_learner_cache(learner_email)
        if cached:
            name = name or cached["name"]
            phone = phone or cached["phone"]
            course = course or cached["course"]

    if email_is_external and (not name or not phone or not course):
        hist = _lookup_from_tickets(learner_email)
        if hist:
            name = name or hist["name"]
            phone = phone or hist["phone"]
            course = course or hist["course"]

    if email_is_external:
        cs = course_master.lookup_by_email(learner_email)
        if cs:
            if cs["name"]:
                name = cs["name"]
            if cs["course"]:
                course = cs["course"]
                course_from_sheet = True

    if email_is_external and (not name or not phone):
        rev = revenue.fetch_revenue_data(learner_email)
        if rev:
            stats["revenue_used"] = True
            name = name or rev["name"]
            phone = phone or rev["phone"]

    if not course_from_sheet and phone:
        cs_phone = course_master.lookup_by_phone(phone)
        if cs_phone:
            if cs_phone["course"]:
                course = cs_phone["course"]
                course_from_sheet = True
            if cs_phone["name"] and not name:
                name = cs_phone["name"]
    # Phone: master sheet is the LAST resort, after Zoho extraction + Revenue.
    if not phone and email_is_external:
        cs_email = course_master.lookup_by_email(learner_email)
        if cs_email and cs_email["phone"]:
            phone = extraction.normalise_phone(cs_email["phone"])

    missing = []
    if not name:
        missing.append("learner_name")
    if not phone:
        missing.append("learner_phone")
    if not course:
        missing.append("course_name")

    meaningful = [c for c in conversations if (c.get("text") or "").strip()]
    total_chars = len(details["description_text"] or "") + sum(len(c["text"]) for c in meaningful)
    has_image_attachments = any(
        any(_guess_image(a) for a in (c.get("attachments") or [])) for c in conversations)
    trivial = (settings.trivial_summary_enabled and not missing and not has_image_attachments
              and len(meaningful) <= settings.trivial_max_messages and total_chars <= settings.trivial_max_chars)

    want_extraction = settings.ai_enabled and settings.ai_extraction_enabled and bool(missing)
    want_summary = settings.ai_enabled and settings.ai_summary_enabled and not trivial
    summary = ""

    if want_extraction or want_summary:
        ai = _lookup_ai_cache(ticket["id"], ticket["modifiedTime"])
        if ai is None:
            ai = extraction.extract_with_ai(
                details, conversations, missing if want_extraction else [], want_summary,
                {"name": name, "phone": phone, "course": course})
            stats["ai_used"] = True
            _upsert_ai_cache(ticket["id"], ticket["modifiedTime"], ai)
        name = name or ai.get("learner_name", "")
        phone = phone or ai.get("learner_phone", "")
        course = course or ai.get("course_name", "")
        summary = ai.get("requirement_summary", "")
        stats["confidence"] = ai.get("confidence", "")

    if not summary:
        summary = extraction.build_deterministic_summary(details, conversations)

    if course and not course_from_sheet:
        course = extraction.normalise_course_name(course)

    if email_is_external:
        _upsert_learner_cache(learner_email, name, phone, course)

    category = ""

    return {
        "row": {
            "ticket": ticket["ticketNumber"], "ticket_id": str(ticket["id"]),
            "owner": details["owner_name"], "category": category,
            "created_at": _to_iso(details["created_time"]),
            "brand": ticket.get("departmentName", ""), "name": name, "email": learner_email,
            "phone": phone, "course": course, "requirement": summary,
            "zoho_status": details["status"], "modified_time": ticket["modifiedTime"],
        },
        "stats": stats,
    }


def _guess_image(name: str) -> bool:
    ext = (name or "").lower().rsplit(".", 1)[-1]
    return ext in ("png", "jpg", "jpeg", "gif", "webp")


def _to_iso(zoho_time: str) -> str:
    if not zoho_time:
        return ""
    try:
        return _parse_iso(zoho_time).astimezone(settings.tz).isoformat()
    except ValueError:
        return zoho_time


# ---------------------------------------------------------------------------
# Run one sync (port of main())
# ---------------------------------------------------------------------------

def run() -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "another sync is already running"}
    try:
        existing_ids = _existing_ticket_numbers()
        run_started = datetime.now(settings.tz).isoformat()

        candidates = fetch_community_tickets(existing_ids)
        if not candidates:
            set_state(LAST_SYNC_KEY, run_started)
            return {"imported": 0, "failed": 0, "deferred": 0}

        batch = candidates[:settings.max_tickets_per_run]
        processed_all = len(batch) == len(candidates)
        failures = 0

        with get_conn() as c:
            for i, ticket in enumerate(batch):
                started = time.time()
                try:
                    result = process_ticket(ticket)
                    r = result["row"]
                    c.execute(
                        """INSERT OR IGNORE INTO tickets
                           (ticket, ticket_id, owner, created_at, imported_at, brand, name, email,
                            phone, course, requirement, category, zoho_status, modified_time)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (r["ticket"], r["ticket_id"], r["owner"], r["created_at"],
                         datetime.now(settings.tz).isoformat(), r["brand"], r["name"], r["email"],
                         r["phone"], r["course"], r["requirement"], r["category"], r["zoho_status"],
                         r["modified_time"]))
                    existing_ids.add(str(r["ticket"]))
                    c.execute(
                        "INSERT INTO sync_log (at, ticket, email, status, ai_used, revenue_used, confidence, ms, error) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (datetime.now(settings.tz).isoformat(), ticket["ticketNumber"], r["email"], "OK",
                         int(result["stats"]["ai_used"]), int(result["stats"]["revenue_used"]),
                         str(result["stats"]["confidence"]), int((time.time() - started) * 1000), ""))
                except Exception as e:  # noqa: BLE001 — one bad ticket must not stop the batch
                    failures += 1
                    c.execute(
                        "INSERT INTO sync_log (at, ticket, email, status, ai_used, revenue_used, confidence, ms, error) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (datetime.now(settings.tz).isoformat(), ticket["ticketNumber"], ticket.get("email", ""),
                         "ERROR", 0, 0, "", int((time.time() - started) * 1000), str(e)))

                if (i + 1) % settings.tickets_per_pause == 0:
                    time.sleep(settings.pause_ms / 1000)

        try:
            categorize.categorize_new_rows(settings.category_max_requests_per_sync)
        except categorize.CategorizeError:
            pass  # a categorisation failure must never fail the sync

        if processed_all and failures == 0:
            set_state(LAST_SYNC_KEY, run_started)

        return {"imported": len(batch) - failures, "failed": failures,
               "deferred": len(candidates) - len(batch)}
    finally:
        _lock.release()


# ---------------------------------------------------------------------------
# Status/owner refresh (port of refreshRecentTicketStatuses)
# ---------------------------------------------------------------------------

def refresh_statuses() -> dict:
    last_sweep = get_state(STATUS_SYNC_KEY, "")
    if last_sweep:
        cutoff = _parse_iso(last_sweep) - timedelta(minutes=settings.status_overlap_minutes)
    else:
        cutoff = datetime.now(settings.tz) - timedelta(hours=settings.status_first_run_lookback_hours)

    with get_conn() as c:
        by_number = {r["ticket"]: r["id"] for r in c.execute("SELECT id, ticket FROM tickets").fetchall()}

    run_started = datetime.now(settings.tz).isoformat()
    changed = 0
    started = time.time()
    cleanly_reached_cutoff = False

    for page in range(settings.status_max_list_pages):
        if time.time() - started > 270:  # stay well inside a reasonable execution budget
            break
        tickets = zoho.tickets_page(from_=page * 100, limit=100)
        if not tickets:
            cleanly_reached_cutoff = True
            break
        page_reached_cutoff = False
        with get_conn() as c:
            for t in tickets:
                mod = _parse_iso(t.get("modifiedTime") or t.get("createdTime"))
                if mod < cutoff:
                    page_reached_cutoff = True
                    continue
                number = str(t["ticketNumber"])
                if number not in by_number:
                    continue
                owner = zoho.owner_label(t)
                status = t.get("status", "")
                cur = c.execute(
                    "UPDATE tickets SET owner=?, zoho_status=? WHERE id=? AND (owner != ? OR zoho_status != ?)",
                    (owner, status, by_number[number], owner, status))
                changed += cur.rowcount
        if page_reached_cutoff or len(tickets) < 100:
            cleanly_reached_cutoff = True
            break

    if cleanly_reached_cutoff:
        set_state(STATUS_SYNC_KEY, run_started)

    return {"changed": changed}
