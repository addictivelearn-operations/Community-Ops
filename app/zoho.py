"""
Zoho Desk — token minting and the handful of Desk calls the app makes.

The app mints its own access tokens from the Self Client refresh token in
.env, the same credentials the token bridge uses. That works from an ordinary
machine; it is only Google's IP ranges Zoho refuses (HANDOVER.txt §4) — which
is also why the live version of this app must not be hosted on Google Cloud.

Credentials always travel in the POST body, never a query string: a URL ends
up in exception messages, and a leaked one is how this project once wrote its
client secret into a spreadsheet.
"""

import html
import re
import time
from datetime import datetime

import httpx

from .config import settings

HTTP_TIMEOUT = 40
TOKEN_MARGIN = 5 * 60          # refresh this long before expiry
FAIL_BACKOFF = 120             # after a failed mint, do not hammer accounts.zoho


class ZohoError(RuntimeError):
    pass


_token: dict = {"value": "", "expires": 0.0, "failed_until": 0.0}


def access_token() -> str:
    now = time.time()
    if _token["value"] and now < _token["expires"] - TOKEN_MARGIN:
        return _token["value"]
    if now < _token["failed_until"]:
        raise ZohoError("Zoho token refresh failed recently; retrying in a moment.")
    r = httpx.post(f"{settings.zoho_accounts}/oauth/v2/token", data={
        "grant_type": "refresh_token",
        "client_id": settings.zoho_client_id,
        "client_secret": settings.zoho_client_secret,
        "refresh_token": settings.zoho_refresh_token,
    }, timeout=HTTP_TIMEOUT)
    body = r.json() if r.text else {}
    if "access_token" not in body:
        _token["failed_until"] = now + FAIL_BACKOFF
        raise ZohoError(f"Zoho refused the token refresh: {str(body)[:200]}")
    _token["value"] = body["access_token"]
    _token["expires"] = now + int(body.get("expires_in", 3600))
    return _token["value"]


def _headers() -> dict:
    return {"Authorization": f"Zoho-oauthtoken {access_token()}",
            "orgId": settings.zoho_org_id}


def _request(method: str, path: str, payload: dict | None = None) -> httpx.Response:
    url = settings.zoho_desk_base + path
    delay = 1.0
    for attempt in range(4):
        r = httpx.request(method, url, headers=_headers(), json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 3:
                break
            retry_after = r.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else delay)
            delay *= 2
            continue
        return r
    raise ZohoError(f"Zoho {method} {path.split('?')[0]} kept failing (HTTP {r.status_code}): {r.text[:300]}")


def get(path: str) -> dict | None:
    r = _request("GET", path)
    if r.status_code == 204:
        return None
    if r.status_code == 404:
        return None
    if r.status_code >= 300:
        raise ZohoError(f"Zoho GET {path.split('?')[0]} failed (HTTP {r.status_code}): {r.text[:300]}")
    return r.json()


def write(method: str, path: str, payload: dict | None) -> dict | None:
    r = _request(method, path, payload)
    if r.status_code >= 300:
        detail = r.text[:400] or "(empty response body)"
        if r.status_code == 401:
            detail += " — the token lacks this scope or has expired."
        raise ZohoError(f"Zoho {method} {path.split('?')[0]} failed (HTTP {r.status_code}): {detail}")
    if r.status_code == 204 or not r.text:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def ticket_id_for_number(ticket_number: str) -> str | None:
    body = get(f"/tickets/search?ticketNumber={ticket_number}&limit=1")
    hit = (body or {}).get("data", [None])[0] if body and body.get("data") else None
    return str(hit["id"]) if hit and hit.get("id") else None


def ticket(ticket_id: str) -> dict | None:
    return get(f"/tickets/{ticket_id}")


def ticket_full(ticket_id: str) -> dict | None:
    """Full detail with contact + assignee embedded — port of
    fetchTicketDetails_'s underlying call."""
    return get(f"/tickets/{ticket_id}?include=contacts,assignee")


def send_reply(ticket_id: str, to_email: str, from_address: str, html_body: str) -> dict | None:
    return write("POST", f"/tickets/{ticket_id}/sendReply", {
        "channel": "EMAIL",
        "to": to_email,
        "fromEmailAddress": from_address,
        "contentType": "html",
        "content": html_body,
        "isForward": False,
    })


def close_ticket(ticket_id: str, unassign: bool = False) -> dict | None:
    payload: dict = {"status": settings.reply_close_status}
    if unassign:
        payload["assigneeId"] = None
    return write("PATCH", f"/tickets/{ticket_id}", payload)


def create_ticket(*, subject: str, department_id: str, name: str, email: str,
                  phone: str | None) -> dict:
    parts = name.split()
    contact = {
        "lastName": (" ".join(parts[1:]) if len(parts) > 1 else name) or email,
        "email": email,
    }
    if len(parts) > 1:
        contact["firstName"] = parts[0]
    if phone:
        contact["phone"] = phone
    body = write("POST", "/tickets", {
        "subject": subject, "departmentId": department_id,
        "channel": "Email", "contact": contact,
    })
    if not body or not body.get("id"):
        raise ZohoError("Zoho returned no ticket id")
    return body


_departments: dict = {}


def department_map() -> dict[str, str]:
    if not _departments:
        body = get("/departments?limit=100") or {}
        for d in body.get("data", []):
            _departments[str(d["id"])] = d.get("name", "")
    return _departments


_agents: dict = {}


def agent_map() -> dict[str, str]:
    """agentId -> display name, for tickets the LIST endpoint returns only an
    assigneeId for (03_ZohoApi.gs's getAgentMap_)."""
    if not _agents:
        try:
            body = get("/agents?limit=200") or {}
            for a in body.get("data", []):
                name = f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                _agents[str(a["id"])] = name or a.get("email", "")
        except ZohoError:
            pass  # an unavailable agent list must never break a sync
    return _agents


def tickets_page(from_: int, limit: int = 100, sort: str = "-modifiedTime",
                 include: str = "assignee,team") -> list[dict]:
    """One page of the ticket LIST endpoint — used for both ticket discovery
    (01_Main.gs's fetchCommunityTickets_) and the status/owner refresh sweep
    (12_TicketLinks.gs's refreshRecentTicketStatuses)."""
    body = get(f"/tickets?limit={limit}&from={from_}&sortBy={sort}&include={include}") or {}
    return body.get("data", [])


def team_agent_id() -> str | None:
    """Community Team's own agent id, resolved by display name out of
    agent_map() (22 Sep 2026) rather than hardcoded, so it keeps working if
    that agent account is ever recreated. None if no agent's name matches
    settings.team_name."""
    name_lc = settings.team_name.strip().lower()
    for aid, name in agent_map().items():
        if name.strip().lower() == name_lc:
            return aid
    return None


def tickets_by_assignee(assignee_id: str, from_: int, limit: int = 100) -> list[dict]:
    """One page of /tickets/search filtered to a single assignee (22 Sep
    2026) — unlike tickets_page (the LIST endpoint, ordered by the
    org's-always-null modifiedTime), this returns tickets by CURRENT
    assignment, independent of any timestamp. Confirmed live: assigneeId is
    accepted here though the plain /tickets LIST endpoint rejects it. Used
    to catch a ticket reassigned into Community Team from another
    department, which createdTime-based discovery can never see since
    createdTime doesn't change on reassignment."""
    body = get(f"/tickets/search?assigneeId={assignee_id}&limit={limit}&from={from_}") or {}
    return body.get("data", [])


def owner_label(t: dict) -> str:
    """Column B — port of ownerLabel_/agentDisplayName_. An assigned ticket
    gives the owner's name alone; only an unassigned one is qualified by
    department, since department is the only thing then telling one blank
    owner from another."""
    assignee = t.get("assignee") or {}
    name = f"{assignee.get('firstName', '')} {assignee.get('lastName', '')}".strip()
    if not name:
        name = assignee.get("email", "")
    if not name and t.get("assigneeId"):
        name = agent_map().get(str(t["assigneeId"]), "")
    if name:
        return name
    dept = department_map().get(str(t.get("departmentId", "")), "")
    return f"Unassigned ({dept})" if dept else "Unassigned"


# ---------------------------------------------------------------------------
# Conversations (for the thread viewer)
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|tr|li|h[1-6]|blockquote)[^>]*>", re.I)


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", raw)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def conversations(ticket_id: str) -> list[dict]:
    body = get(f"/tickets/{ticket_id}/conversations?limit=100") or {}
    out = []
    for entry in body.get("data", []):
        try:
            if entry.get("type") == "thread":
                full = get(f"/tickets/{ticket_id}/threads/{entry['id']}") or {}
                status = str(full.get("status") or entry.get("status") or "")
                author = full.get("author") or {}
                out.append({
                    "type": "email",
                    "direction": full.get("direction") or entry.get("direction") or "",
                    "is_draft": bool(full.get("isDraft") or entry.get("isDraft") or status.upper() == "DRAFT"),
                    "author": author.get("name") or full.get("fromEmailAddress") or "",
                    "from_email": full.get("fromEmailAddress") or "",
                    "author_email": author.get("email") or "",
                    "to": full.get("to") or "",
                    "cc": full.get("cc") or "",
                    "time": full.get("createdTime") or entry.get("createdTime") or "",
                    "text": html_to_text(full.get("content") or full.get("summary") or ""),
                    "attachments": [a.get("name", "") for a in full.get("attachments", [])],
                })
            elif entry.get("type") == "comment":
                out.append({
                    "type": "comment",
                    "direction": "public" if entry.get("isPublic") else "private",
                    "author": (entry.get("commenter") or {}).get("name", ""),
                    "time": entry.get("commentedTime") or "",
                    "text": html_to_text(entry.get("content") or ""),
                    "attachments": [a.get("name", "") for a in entry.get("attachments", [])],
                })
        except ZohoError:
            continue
    out.sort(key=lambda c: c["time"])
    return out


def fmt_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.astimezone(settings.tz).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso


def check() -> dict:
    """Connectivity proof for the diagnostics page."""
    out = {}
    try:
        t = access_token()
        out["accounts"] = f"OK — token of {len(t)} chars"
    except Exception as e:  # noqa: BLE001
        out["accounts"] = f"FAILED — {e}"
        return out
    try:
        body = get("/tickets?limit=1")
        out["desk"] = "OK" if body is not None else "OK (empty)"
    except Exception as e:  # noqa: BLE001
        out["desk"] = f"FAILED — {e}"
    return out
