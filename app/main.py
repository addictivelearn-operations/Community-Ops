"""
community-ops-app — the web app. Routes only; the workflows live in
refunds.py and replies.py, the integrations in google.py and zoho.py.

Run:  uvicorn app.main:app --reload --port 8000   (or run.bat)
"""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as fastapi_default_http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import course_master, google, migrate, refund_intake, refunds, replies, scheduler, tracker_app, zoho, zoho_sync
from .config import RF_CURRENCY, settings
from .db import get_conn, get_state, init_db, set_state
from .store import (MAIN_EDITABLE, MAIN_HEADERS, REFUND_EDITABLE, REFUND_HEADERS, STATUS_FALLBACK,
                    TRIGGER_FALLBACK, create_test_refund, create_test_ticket, delete_refund,
                    delete_ticket, is_internal, load_refund, load_refunds, load_ticket,
                    load_tickets, parse_amount, save_main_fields, save_refund_fields, write_refund)

init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.stop()


HERE = Path(__file__).resolve().parent
app = FastAPI(title="Community Ops", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret, same_site="lax",
                   https_only=settings.base_url.startswith("https"))
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(settings=settings, zoho_time=zoho.fmt_time)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class User:
    def __init__(self, email: str, name: str):
        self.email = email
        self.name = name
        self.display = settings.authorized_senders.get(email.lower(), name)

    @property
    def can_edit(self) -> bool:
        """Anyone on an allowed domain can sign in and view (google.is_allowed);
        only an AUTHORIZED_SENDER may change anything — same list that already
        governed who could approve/send, now also gating inline edits and the
        admin sync buttons."""
        return self.email.lower() in settings.authorized_senders

    @property
    def is_superuser(self) -> bool:
        """The one account that may add a test entry or delete any entry —
        narrower than can_edit, which every AUTHORIZED_SENDER has (21 Sep 2026)."""
        return self.email.lower() == settings.superuser_email


def current_user(request: Request) -> User | None:
    s = request.session
    if s.get("email"):
        return User(s["email"], s.get("name", s["email"]))
    return None


def require_user(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=307, headers={"Location": "/login?" + urlencode({"next": str(request.url.path)})})
    return u


def require_editor(u: User = Depends(require_user)) -> User:
    if not u.can_edit:
        raise HTTPException(status_code=403,
                            detail=f"{u.email} has view-only access. Ask an editor "
                                   f"({', '.join(sorted(settings.authorized_senders))}) to make this change.")
    return u


def require_superuser(u: User = Depends(require_user)) -> User:
    if not u.is_superuser:
        raise HTTPException(status_code=403,
                            detail=f"{u.email} cannot do this. Only {settings.superuser_email} "
                                   f"can add or delete entries.")
    return u


def gclient(u: User) -> google.GoogleClient:
    try:
        return google.GoogleClient(u.email)
    except google.GoogleError as e:
        raise HTTPException(status_code=307, headers={"Location": "/login?" + urlencode({"msg": str(e)})})


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    u = current_user(request)
    ctx.setdefault("user", u)
    ctx.setdefault("can_edit", bool(u and u.can_edit))
    ctx.setdefault("is_superuser", bool(u and u.is_superuser))
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("ok", request.query_params.get("ok", "") == "1")
    return templates.TemplateResponse(request, name, ctx)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Same as FastAPI's default, except a 403 (require_editor) gets a
    response shaped for whoever's asking — JSON for the inline-edit
    endpoints (their own JS expects {"ok", "msg"}, not an HTML page), the
    app's own error page for everything else — instead of a bare JSON
    {"detail": ...} body. Everything else (redirects, 404s) behaves exactly
    as before."""
    if exc.status_code == 403:
        if request.url.path.endswith("/cell"):
            return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=403)
        resp = render(request, "error.html", error=str(exc.detail))
        resp.status_code = 403
        return resp
    return await fastapi_default_http_exception_handler(request, exc)


PAGE_SIZE = 300


def paginate(request: Request, rows: list) -> dict:
    """Slices rows for ?page=N (1-based) and returns what the template needs."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    start = (page - 1) * PAGE_SIZE
    return {"rows": rows[start:start + PAGE_SIZE], "total": total, "page": page, "pages": pages,
            "first": start + 1 if total else 0, "last": min(start + PAGE_SIZE, total)}


def back(url: str, msg: str, ok: bool) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}{urlencode({'msg': msg, 'ok': '1' if ok else '0'})}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return render(request, "login.html", missing=settings.missing(),
                  next=request.query_params.get("next", "/"))


@app.get("/auth/google")
def auth_google(request: Request):
    state = google.new_state()
    request.session["oauth_state"] = state
    request.session["next"] = request.query_params.get("next", "/")
    return RedirectResponse(google.auth_url(state))


@app.get("/auth/callback")
def auth_callback(request: Request):
    if request.query_params.get("state") != request.session.get("oauth_state"):
        return back("/login", "Sign-in state mismatch — try again.", False)
    if "error" in request.query_params:
        return back("/login", "Google refused: " + request.query_params["error"], False)
    try:
        ident = google.exchange_code(request.query_params["code"])
    except google.GoogleError as e:
        return back("/login", str(e), False)
    allowed, why = google.is_allowed(ident["email"])
    if not allowed or not ident["email_verified"]:
        return back("/login", why or "Email not verified by Google.", False)
    google.save_user(ident["email"], ident["name"], ident["refresh_token"],
                     ident["access_token"], ident["expires_in"])
    request.session.clear()
    request.session["email"] = ident["email"]
    request.session["name"] = ident["name"]
    return RedirectResponse(request.session.pop("next", "/") or "/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, u: User = Depends(require_user)):
    rf = load_refunds()
    tk = load_tickets()
    return render(request, "home.html",
                  refund_pending=sum(1 for r in rf if r.pending),
                  refund_new=sum(1 for r in rf if not r.trigger and not r.sent),
                  refund_total=len(rf),
                  reply_pending=sum(1 for t in tk if t.pending),
                  reply_total=len(tk))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _ticket_status(sent_at: str, trigger: str) -> str:
    if sent_at:
        return "sent"
    if (trigger or "").strip().upper() == "NA":
        return "na"
    return "pending"


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, u: User = Depends(require_superuser)):
    """Two read-only breakdowns (Ticket replies, Refunds) on one tab, each
    with its own day/month filter. All aggregation happens client-side —
    both tables are small enough (low thousands of rows) to ship whole and
    filter/recompute instantly in JS rather than round-tripping to the
    server on every filter change."""
    with get_conn() as c:
        tickets = c.execute("SELECT created_at, course, category, trigger_value, sent_at, "
                            "brand, owner, zoho_status FROM tickets").fetchall()
        refunds = c.execute("SELECT timestamp_at, community, amount FROM refunds").fetchall()

    ticket_data = []
    for t in tickets:
        day = (t["created_at"] or "")[:10]
        if not day:
            continue
        ticket_data.append({"day": day, "month": day[:7],
                            "course": t["course"].strip() if t["course"] else "(blank)",
                            "category": t["category"].strip() if t["category"] else "(uncategorised)",
                            "status": _ticket_status(t["sent_at"], t["trigger_value"]),
                            "brand": t["brand"].strip() if t["brand"] else "(blank)",
                            "agent": t["owner"].strip() if t["owner"] else "(unassigned)",
                            "zohoStatus": t["zoho_status"].strip() if t["zoho_status"] else "(blank)"})

    refund_data = []
    for r in refunds:
        day = (r["timestamp_at"] or "")[:10]
        if not day:
            continue
        refund_data.append({"day": day, "month": day[:7],
                            "group": r["community"].strip() if r["community"] else "(blank)",
                            "amount": parse_amount(r["amount"])})

    def to_json(data: list) -> str:
        return json.dumps(data).replace("</", "<\\/")

    return render(request, "dashboard.html", currency=RF_CURRENCY,
                  tickets_json=to_json(ticket_data), refunds_json=to_json(refund_data))


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

@app.get("/refunds", response_class=HTMLResponse)
def refunds_list(request: Request, u: User = Depends(require_user)):
    view = request.query_params.get("view", "open")
    q = request.query_params.get("q", "").strip().lower()
    rows = load_refunds()
    if view == "open":
        rows = [r for r in rows if not r.sent]
    elif view == "sent":
        rows = [r for r in rows if r.sent]
    if q:
        rows = [r for r in rows if q in (r.name + " " + r.email + " " + r.community + " " + r.phone).lower()]
    rows.reverse()
    return render(request, "refunds.html", view=view, q=q, headers=REFUND_HEADERS,
                  trigger_options=TRIGGER_FALLBACK,
                  **paginate(request, rows))


def _apply_refund_trigger(g: google.GoogleClient, u: User, row: int, value: str) -> tuple[str, bool]:
    """The trigger field set from the app. "Yes" on an unsent row SENDS;
    anything else is just recorded. `g` is only needed for the send path,
    which drives the finance-team handoff."""
    r = load_refund(row)
    if not r:
        return f"Row {row} is empty", False
    if value.lower() == "yes" and not r.sent:
        first, second = refunds.approve(g, row, u.display, u.email)
        msg = "Learner: " + first.message + (f" · Team: {second.message}" if second else "")
        return msg, first.ok and (second is None or second.ok)
    if value != r.trigger:
        write_refund(row, "trigger", value)
        return f'Row {row}: Trigger set to "{value or "(blank)"}"' + (" — already sent, nothing re-sent." if r.sent else "."), True
    return f"Row {row}: no change.", True


@app.post("/refunds/{row}/cell")
def refund_cell(row: int, field: str = Form(...), value: str = Form(""), u: User = Depends(require_editor)):
    """Inline edit from the list (community). Never sends."""
    if field not in REFUND_EDITABLE or field == "trigger":
        return JSONResponse({"ok": False, "msg": "not editable"}, status_code=400)
    written = save_refund_fields(row, {field: value.strip()})
    return JSONResponse({"ok": True, "msg": ("saved " + ", ".join(written)) if written else "no change"})


@app.post("/refunds/{row}/community")
def refund_community(row: int, value: str = Form(""), u: User = Depends(require_editor)):
    written = save_refund_fields(row, {"community": value.strip()})
    return back(f"/refunds/{row}", "Saved community." if written else "No change.", True)


@app.post("/refunds/{row}/trigger")
def refund_trigger(request: Request, row: int, value: str = Form(""), back_to: str = Form("/refunds"),
                   u: User = Depends(require_editor)):
    msg, ok = _apply_refund_trigger(gclient(u), u, row, value.strip())
    return back(back_to if back_to.startswith("/") else "/refunds", msg, ok)


@app.get("/refunds/new", response_class=HTMLResponse)
def refund_new_form(request: Request, u: User = Depends(require_superuser)):
    return render(request, "refund_new.html")


@app.post("/refunds/new")
def refund_new(name: str = Form(""), email: str = Form(""), phone: str = Form(""),
               group: str = Form(""), reason: str = Form(""), funnel: str = Form(""),
               funnel_final: str = Form(""), community: str = Form(""), amount: str = Form(""),
               u: User = Depends(require_superuser)):
    row = create_test_refund({"name": name, "email": email, "phone": phone, "group": group,
                              "reason": reason, "funnel": funnel, "funnel_final": funnel_final,
                              "community": community, "amount": amount})
    return RedirectResponse(f"/refunds/{row}?msg={quote('Test entry added.')}&ok=1", status_code=303)


@app.get("/refunds/{row}", response_class=HTMLResponse)
def refund_detail(request: Request, row: int, u: User = Depends(require_user)):
    r = load_refund(row)
    if not r:
        raise HTTPException(404, f"Row {row} is empty")
    d = refunds.handoff_data(r, u.display, u.email)
    tracker_lookup = None
    if u.is_superuser and tracker_app.configured():
        try:
            tracker_lookup = tracker_app.find(r.key)
        except tracker_app.TrackerError as e:
            tracker_lookup = {"error": str(e)}
    return render(request, "refund_detail.html", r=r,
                  headers=REFUND_HEADERS,
                  trigger_options=TRIGGER_FALLBACK,
                  blockers=r.blockers(),
                  learner_subject=refunds.learner_subject(r),
                  learner_html=refunds.learner_email_html(r),
                  team_html=refunds.team_email_html(d, "(doc link)"),
                  recipients=refunds.recipients(),
                  reply_to=d.approved_by_email,
                  tracker_lookup=tracker_lookup)


@app.post("/refunds/{row}/approve")
def refund_approve(row: int, u: User = Depends(require_editor)):
    g = gclient(u)
    first, second = refunds.approve(g, row, u.display, u.email)
    msg = ("Learner: " + first.message) + (f" · Team: {second.message}" if second else "")
    return back(f"/refunds/{row}", msg, first.ok and (second is None or second.ok))


@app.post("/refunds/{row}/handoff")
def refund_handoff(row: int, u: User = Depends(require_editor)):
    g = gclient(u)
    r = load_refund(row)
    if not r:
        raise HTTPException(404)
    if not r.sent:
        return back(f"/refunds/{row}", "The learner has not been emailed yet — approve first.", False)
    out = refunds.handoff_and_record(g, r, u.display, u.email)
    return back(f"/refunds/{row}", "Team: " + out.message, out.ok)


@app.post("/refunds/{row}/handoff-force")
def refund_handoff_force(row: int, u: User = Depends(require_superuser)):
    """Force-resend the finance email even if the tracker app (or sheet)
    already has it marked Sent — deliberately bypasses the never-twice
    guard, for retesting a row (or fixing a genuinely wrong send) rather
    than the normal Retry, which only fills what's missing. Superuser only:
    a real row's finance team could get a duplicate email if misused."""
    g = gclient(u)
    r = load_refund(row)
    if not r:
        raise HTTPException(404)
    if not r.sent:
        return back(f"/refunds/{row}", "The learner has not been emailed yet — approve first.", False)
    out = refunds.handoff_and_record(g, r, u.display, u.email, force=True)
    return back(f"/refunds/{row}", "Team (forced): " + out.message, out.ok)


@app.post("/refunds/{row}/resend")
def refund_resend(row: int, u: User = Depends(require_editor)):
    out = refunds.resend_learner_email(row, u.display)
    return back(f"/refunds/{row}", "Learner: " + out.message, out.ok)


@app.post("/refunds/{row}/delete")
def refund_delete(row: int, u: User = Depends(require_superuser)):
    ok = delete_refund(row)
    return back("/refunds", f"Row {row} deleted." if ok else f"Row {row} was already gone.", ok)


# ---------------------------------------------------------------------------
# Ticket replies
# ---------------------------------------------------------------------------

@app.get("/replies", response_class=HTMLResponse)
def replies_list(request: Request, u: User = Depends(require_user)):
    view = request.query_params.get("view", "pending")
    q = request.query_params.get("q", "").strip().lower()
    rows = load_tickets()
    if view == "pending":
        rows = [t for t in rows if t.pending]
    elif view == "unsent":
        rows = [t for t in rows if not t.sent]
    elif view == "sent":
        rows = [t for t in rows if t.sent]
    if q:
        rows = [t for t in rows if q in (t.ticket + " " + t.name + " " + t.email + " " + t.course + " " + t.requirement).lower()]
    rows.reverse()
    return render(request, "replies.html", view=view, q=q, headers=MAIN_HEADERS,
                  trigger_options=TRIGGER_FALLBACK, status_options=STATUS_FALLBACK,
                  **paginate(request, rows))


@app.post("/replies/{row}/cell")
def reply_cell(row: int, field: str = Form(...), value: str = Form(""), u: User = Depends(require_editor)):
    """Inline edit from the list: one field (resolution, status, …), saved
    when the agent clicks away. Never sends — Trigger has its own route."""
    if field not in MAIN_EDITABLE or field == "trigger":
        return JSONResponse({"ok": False, "msg": "not editable"}, status_code=400)
    written = save_main_fields(row, {field: value.strip()})
    return JSONResponse({"ok": True, "msg": ("saved " + ", ".join(written)) if written else "no change"})


def _apply_reply_trigger(u: User, row: int, value: str) -> tuple[str, bool]:
    """The trigger field set from the app. "Yes" on an unsent row SENDS."""
    t = load_ticket(row)
    if not t:
        return f"Row {row} is empty", False
    if value.lower() == "yes" and not t.sent:
        out = replies.send(row, u.display)
        return out.message, out.ok
    if value != t.trigger:
        save_main_fields(row, {"trigger": value})
        return f'Row {row}: Trigger set to "{value or "(blank)"}"' + (" — already sent, nothing re-sent." if t.sent else "."), True
    return f"Row {row}: no change.", True


@app.post("/replies/{row}/trigger")
def reply_trigger(request: Request, row: int, value: str = Form(""), back_to: str = Form("/replies"),
                  u: User = Depends(require_editor)):
    msg, ok = _apply_reply_trigger(u, row, value.strip())
    return back(back_to if back_to.startswith("/") else "/replies", msg, ok)


@app.get("/replies/new", response_class=HTMLResponse)
def reply_new_form(request: Request, u: User = Depends(require_superuser)):
    return render(request, "reply_new.html")


@app.post("/replies/new")
def reply_new(ticket: str = Form(""), owner: str = Form(""), brand: str = Form(""),
              name: str = Form(""), email: str = Form(""), phone: str = Form(""),
              course: str = Form(""), requirement: str = Form(""),
              u: User = Depends(require_superuser)):
    try:
        row = create_test_ticket({"ticket": ticket, "owner": owner, "brand": brand, "name": name,
                                  "email": email, "phone": phone, "course": course,
                                  "requirement": requirement})
    except sqlite3.IntegrityError:
        return RedirectResponse(f"/replies/new?msg={quote('That ticket number is already in use — leave it blank to auto-generate one.')}&ok=0", status_code=303)
    return RedirectResponse(f"/replies/{row}?msg={quote('Test entry added.')}&ok=1", status_code=303)


@app.get("/replies/{row}", response_class=HTMLResponse)
def reply_detail(request: Request, row: int, u: User = Depends(require_user)):
    t = load_ticket(row)
    if not t:
        raise HTTPException(404, f"Row {row} is empty")
    return render(request, "reply_detail.html", t=t, blockers=replies.blockers(t),
                  preview=replies.reply_html(t), from_address=replies.from_address(t.brand),
                  trigger_options=TRIGGER_FALLBACK, status_options=STATUS_FALLBACK)


@app.post("/replies/{row}/save")
def reply_save(row: int, course: str = Form(""), requirement: str = Form(""), resolution: str = Form(""),
               res_status: str = Form(""), trigger: str = Form(""),
               u: User = Depends(require_editor)):
    """Saves every editable field, then — if Trigger is "Yes" and the row is
    unsent — sends, exactly as choosing Yes on the list would."""
    fields = {"course": course.strip(), "requirement": requirement.strip(),
              "resolution": resolution.strip(), "res_status": res_status.strip()}
    written = save_main_fields(row, fields)
    msg, ok = _apply_reply_trigger(u, row, trigger.strip())
    if written:
        msg = f"Saved {', '.join(written)}. " + msg
    return back(f"/replies/{row}", msg, ok)


@app.post("/replies/{row}/send")
def reply_send(row: int, resend: str = Form(""), u: User = Depends(require_editor)):
    out = replies.send(row, u.display, allow_resend=resend == "1")
    return back(f"/replies/{row}", out.message, out.ok)


@app.post("/replies/{row}/delete")
def reply_delete(row: int, u: User = Depends(require_superuser)):
    ok = delete_ticket(row)
    return back("/replies", f"Row {row} deleted." if ok else f"Row {row} was already gone.", ok)


@app.get("/tickets/{number}", response_class=HTMLResponse)
def ticket_thread(request: Request, number: str, u: User = Depends(require_user)):
    ticket_id = replies.resolve_ticket_id(number)
    if not ticket_id:
        return render(request, "error.html", error=f"Ticket #{number} not found in Zoho Desk")
    try:
        t = zoho.ticket(ticket_id) or {}
        convs = zoho.conversations(ticket_id)
        dept = zoho.department_map().get(str(t.get("departmentId", "")), "")
    except zoho.ZohoError as e:
        return render(request, "error.html", error=str(e))
    blocks = []
    if t.get("description"):
        blocks.append({"cls": "desc", "who": "Original request", "when": t.get("createdTime", ""),
                       "text": zoho.html_to_text(t["description"]), "attachments": [], "tag": ""})
    for c in convs:
        if c["type"] == "email":
            internal = is_internal(c["from_email"]) or is_internal(c["author_email"])
            if c["is_draft"]:
                cls, who, tag = "drf", f"{c['author'] or 'Support'} (support)", "DRAFT — NEVER SENT"
            elif c["direction"] == "out" or internal:
                cls, who, tag = "out", f"{c['author'] or 'Support'} (support)", ""
            else:
                cls, who, tag = "in", f"{c['author'] or c['from_email'] or 'Learner'} (learner)", ""
        else:
            cls = "prv" if c["direction"] == "private" else "cmt"
            who = (c["author"] or "Agent") + (" (internal note)" if cls == "prv" else " (comment)")
            tag = ""
        blocks.append({"cls": cls, "who": who, "when": c["time"], "text": c["text"],
                       "attachments": c["attachments"], "tag": tag})
    blocks.reverse()
    contact = t.get("contact") or {}
    return render(request, "ticket.html", number=number, t=t, dept=dept, blocks=blocks,
                  contact=f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip())


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.post("/admin/migrate")
def admin_migrate(u: User = Depends(require_editor)):
    """One-off: copy every row still only in the two sheets into the app's
    own database. Safe to run more than once — already-migrated rows are
    skipped, not duplicated (see migrate.py)."""
    g = gclient(u)
    report = migrate.run_migration(g)
    msg = (f"Tickets: {report.tickets_migrated} migrated, {report.tickets_skipped} already there"
          f"{f', {len(report.tickets_failed)} FAILED' if report.tickets_failed else ''}. "
          f"Refunds: {report.refunds_migrated} migrated, {report.refunds_skipped} already there"
          f"{f', {len(report.refunds_failed)} FAILED' if report.refunds_failed else ''}.")
    if report.tickets_failed or report.refunds_failed:
        msg += " Failures: " + "; ".join(report.tickets_failed + report.refunds_failed)[:500]
    return back("/diagnostics", msg, not (report.tickets_failed or report.refunds_failed))


@app.post("/admin/resync-replies")
def admin_resync_replies(u: User = Depends(require_editor)):
    """Pull the sheet's CURRENT reply-workflow columns (course, requirement,
    resolution, res_status, trigger, sent_at, sent_by, category, result)
    into the database for tickets already there — for when the sheet, not
    the app, was used to reply/send. See migrate.resync_ticket_replies."""
    g = gclient(u)
    report = migrate.resync_ticket_replies(g)
    msg = f"{report.updated} ticket(s) updated, {report.unchanged} already matched, {report.inserted} new"
    if report.failed:
        msg += f", {len(report.failed)} FAILED: " + "; ".join(report.failed)[:500]
    return back("/diagnostics", msg, not report.failed)


# ---------------------------------------------------------------------------
# Sync pipeline — manual triggers, for testing without waiting on the
# schedule.
# ---------------------------------------------------------------------------

@app.post("/admin/sync/tickets")
def admin_sync_tickets(u: User = Depends(require_editor)):
    result = zoho_sync.run()
    return back("/diagnostics", f"Ticket sync: {result}", "failed" not in result or not result.get("failed"))


@app.post("/admin/sync/tickets/backfill")
def admin_sync_tickets_backfill(days: int = Form(3), u: User = Depends(require_superuser)):
    """Rewinds LAST_SYNC_ISO by `days` and runs the ticket sync immediately —
    for recovering tickets the discovery bug (fixed 22 Sep 2026, see
    zoho_sync.py) silently skipped before the fix shipped: run() advanced
    the cursor to "now" even when it found 0 candidates, so anything missed
    is permanently behind the cursor and an ordinary sync will never look at
    it again. Safe to run more than once, and safe to pick a wide `days` —
    discovery is capped at MAX_LIST_PAGES (1000 org-wide tickets) per call
    regardless of how far back the cursor points, and every candidate is
    still deduplicated by ticket number, so nothing already in the database
    gets touched twice. A gap wider than that window (or than BATCH_SIZE
    tickets processed per run) needs this run again — check `deferred` in
    the result and click again if it's not 0."""
    days = max(1, min(days, 120))
    new_cursor = (datetime.now(settings.tz) - timedelta(days=days)).isoformat()
    set_state(zoho_sync.LAST_SYNC_KEY, new_cursor)
    result = zoho_sync.run()
    return back("/diagnostics", f"Backfill (cursor rewound {days}d to {new_cursor}): {result}",
               not result.get("failed"))


@app.post("/admin/sync/tickets/reassigned")
def admin_sync_tickets_reassigned(u: User = Depends(require_editor)):
    result = zoho_sync.run_reassignment_sweep()
    return back("/diagnostics", f"Reassignment sweep: {result}", not result.get("failed"))


@app.post("/admin/sync/statuses")
def admin_sync_statuses(u: User = Depends(require_editor)):
    result = zoho_sync.refresh_statuses()
    return back("/diagnostics", f"Status refresh: {result}", True)


@app.post("/admin/sync/refunds")
def admin_sync_refunds(u: User = Depends(require_editor)):
    result = refund_intake.run()
    return back("/diagnostics", f"Refund intake: {result}", True)


@app.post("/admin/sync/refunds/resync-unsent")
def admin_resync_unsent_refunds(u: User = Depends(require_editor)):
    result = refund_intake.resync_unsent()
    return back("/diagnostics", f"Resync unsent refunds from Refund_Clean: {result}", True)


@app.post("/admin/sync/course-master")
def admin_sync_course_master(u: User = Depends(require_editor)):
    result = course_master.refresh()
    return back("/diagnostics", f"Course master refresh: {result}", True)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Read-only export for the Master Refund Tracker (Hardik, 21 Sep 2026)
# ---------------------------------------------------------------------------
# The tracker shows a read-only "Community Refunds" tab mirroring the Refunds
# page. It cannot sign in with Google, so this one route takes a shared secret
# (EXPORT_API_KEY) in the X-Api-Key header instead. It reads the same rows the
# Refunds page reads and writes nothing. Off (503) until the key is set.

@app.get("/api/refunds.json")
def api_refunds_json(request: Request):
    key = (settings.export_api_key or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="EXPORT_API_KEY is not set on this app")
    sent = (request.headers.get("x-api-key") or "").strip()
    if not sent or sent != key:
        raise HTTPException(status_code=401, detail="a valid X-Api-Key header is required")
    rows = load_refunds()
    out = []
    for r in rows:
        out.append({
            "row": r.row, "submitted": r.timestamp, "submittedMs": r.timestamp_ms,
            "name": r.name, "email": r.email, "phone": r.phone, "groupForm": r.group,
            "reason": r.reason, "funnel": r.funnel, "funnelFinal": r.funnel_final, "noOrder": r.no_order,
            "community": r.community, "amount": r.amount, "brand": r.brand,
            "trigger": r.trigger, "sentAt": r.sent_at, "sentBy": r.sent_by,
            "result": r.result, "handoff": r.handoff, "sent": r.sent,
        })
    return JSONResponse({
        "app": "community-ops", "generatedAt": datetime.now(timezone.utc).isoformat(),
        "headers": [{"letter": a, "label": b} for a, b in REFUND_HEADERS],
        "count": len(out), "rows": out,
    })


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request, u: User = Depends(require_superuser)):
    g = gclient(u)
    checks = {}
    checks["Signed in as"] = f"{u.email} ({u.display})"
    z = zoho.check()
    checks["Zoho accounts"] = z.get("accounts", "")
    checks["Zoho Desk API"] = z.get("desk", "")
    checks["Ticket tracker / Community Refund"] = f"app database — {len(load_tickets())} tickets, {len(load_refunds())} refunds"
    with get_conn() as c:
        course_rows = c.execute("SELECT COUNT(*) AS n FROM course_purchases").fetchone()["n"]
    checks["Course master mirror"] = f"{course_rows} row(s)" + ("" if course_rows else " — run the refresh below")
    checks["Ticket sync cursor"] = get_state(zoho_sync.LAST_SYNC_KEY) or "(never run)"
    checks["Status sweep cursor"] = get_state(zoho_sync.STATUS_SYNC_KEY) or "(never run)"
    checks["Gemini / Revenue configured"] = (
        f"Gemini (extraction fallback + categorisation): {'yes' if settings.gemini_api_key else 'NO — GEMINI_API_KEY not set'}, "
        f"Revenue: {'yes' if settings.revenue_api_url else 'no'}")
    checks["Unattended sync identity"] = settings.sync_service_account_email
    try:
        checks["Team spreadsheet"] = (f'"{g.sheet_title(settings.team_sheet_id)}" — tabs: '
                                      + ", ".join(t["title"] for t in g.sheet_tabs(settings.team_sheet_id)))
    except google.GoogleError as e:
        checks["Team spreadsheet"] = f"CANNOT OPEN — {e}"
    try:
        checks["Doc folder"] = f'"{g.folder_name(settings.team_doc_folder_id)}"'
    except google.GoogleError as e:
        checks["Doc folder"] = f"CANNOT OPEN — {e}"
    rc = refunds.recipients()
    checks["Team email To"] = ", ".join(rc["to"])
    checks["Team email Cc"] = ", ".join(rc["cc"]) or "(none)"
    checks["Team email Bcc"] = ", ".join(rc["bcc"]) or "(none)"
    checks["Team email From"] = f"{u.email} (the signed-in approver)"
    checks["Tracker first auto row"] = str(settings.team_first_auto_row)
    checks["Learner email From"] = settings.rf_from_address
    checks["Reply From (default)"] = settings.reply_default_from
    return render(request, "diagnostics.html", checks=checks, missing=settings.missing())
