"""
community-ops-app — the web app. Routes only; the workflows live in
refunds.py and replies.py, the integrations in google.py and zoho.py.

Run:  uvicorn app.main:app --reload --port 8000   (or run.bat)
"""

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as fastapi_default_http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import course_master, google, migrate, refund_intake, refunds, replies, scheduler, zoho, zoho_sync
from .config import settings
from .db import get_conn, get_state, init_db
from .store import (MAIN_EDITABLE, MAIN_HEADERS, REFUND_EDITABLE, REFUND_HEADERS, STATUS_FALLBACK,
                    TRIGGER_FALLBACK, is_internal, load_refund, load_refunds, load_ticket,
                    load_tickets, save_main_fields, save_refund_fields, write_refund)

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


def gclient(u: User) -> google.GoogleClient:
    try:
        return google.GoogleClient(u.email)
    except google.GoogleError as e:
        raise HTTPException(status_code=307, headers={"Location": "/login?" + urlencode({"msg": str(e)})})


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    u = current_user(request)
    ctx.setdefault("user", u)
    ctx.setdefault("can_edit", bool(u and u.can_edit))
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


@app.get("/refunds/{row}", response_class=HTMLResponse)
def refund_detail(request: Request, row: int, u: User = Depends(require_user)):
    r = load_refund(row)
    if not r:
        raise HTTPException(404, f"Row {row} is empty")
    d = refunds.handoff_data(r, u.display, u.email)
    d.tracker_row = 0
    return render(request, "refund_detail.html", r=r,
                  headers=REFUND_HEADERS,
                  trigger_options=TRIGGER_FALLBACK,
                  blockers=r.blockers(),
                  learner_subject=refunds.learner_subject(r),
                  learner_html=refunds.learner_email_html(r),
                  team_html=refunds.team_email_html(d, "(doc link)", "#"),
                  recipients=refunds.recipients(),
                  reply_to=d.approved_by_email)


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


# ---------------------------------------------------------------------------
# Sync pipeline — manual triggers, for testing without waiting on the
# schedule.
# ---------------------------------------------------------------------------

@app.post("/admin/sync/tickets")
def admin_sync_tickets(u: User = Depends(require_editor)):
    result = zoho_sync.run()
    return back("/diagnostics", f"Ticket sync: {result}", "failed" not in result or not result.get("failed"))


@app.post("/admin/sync/statuses")
def admin_sync_statuses(u: User = Depends(require_editor)):
    result = zoho_sync.refresh_statuses()
    return back("/diagnostics", f"Status refresh: {result}", True)


@app.post("/admin/sync/refunds")
def admin_sync_refunds(u: User = Depends(require_editor)):
    result = refund_intake.run()
    return back("/diagnostics", f"Refund intake: {result}", True)


@app.post("/admin/sync/course-master")
def admin_sync_course_master(u: User = Depends(require_editor)):
    result = course_master.refresh()
    return back("/diagnostics", f"Course master refresh: {result}", True)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request, u: User = Depends(require_user)):
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
    checks["Team email To"] = ", ".join(rc["to"]) + ("   <== RF_TEAM_TEST_TO override" if rc["test"] else "")
    checks["Team email Cc"] = ", ".join(rc["cc"]) or "(none)"
    checks["Team email Bcc"] = ", ".join(rc["bcc"]) or "(none)"
    checks["Team email From"] = f"{u.email} (the signed-in approver)"
    checks["Tracker first auto row"] = str(settings.team_first_auto_row)
    checks["Learner email From"] = settings.rf_from_address
    checks["Reply From (default)"] = settings.reply_default_from
    return render(request, "diagnostics.html", checks=checks, missing=settings.missing())
