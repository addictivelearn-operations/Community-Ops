"""
The Master Refund Tracker app — where the finance hand-off row goes now
(Hardik, 21 Sep 2026), instead of the "Community Refunds Tracker" sheet.

Three small calls, all against the tracker's /api/community-refunds/handoff
with the shared secret in X-Api-Key (TRACKER_API_KEY here, COMMUNITY_OPS_API_KEY
there). The tracker keeps ONE row per Source Key, so a retried hand-off finds
its row and never makes a second one; it raises the entry into its Phase 4 →
🤝 Community Refund tab by itself.

Off when TRACKER_URL / TRACKER_API_KEY are not set — refunds.py then falls
back to the sheet exactly as before.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import settings


class TrackerError(Exception):
    pass


def configured() -> bool:
    return bool(settings.tracker_url and settings.tracker_api_key)


def _call(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    url = settings.tracker_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Api-Key": settings.tracker_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Tracker-Tab": "Community Ops",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise TrackerError(f"tracker app answered HTTP {e.code}" + (f" — {detail}" if detail else "")) from e
    except urllib.error.URLError as e:
        raise TrackerError(f"tracker app unreachable — {e.reason}") from e
    except Exception as e:  # noqa: BLE001
        raise TrackerError(f"tracker app: {e}") from e


def nudge(why: str = "") -> None:
    """Tell the tracker a refund row changed so its Community Refunds tab
    re-reads at once instead of at the next 10-minute pull (Hardik, 21 Sep
    2026: "any update … should be reflected live"). Fire-and-forget on a
    thread; a tracker that is down changes nothing here."""
    if not configured():
        return
    import threading

    def go():
        try:
            _call("POST", "/api/community-refunds/pull", {"why": why or "a refund row changed in Community Ops"})
        except Exception:  # noqa: BLE001 — best effort only
            pass
    threading.Thread(target=go, daemon=True).start()


def find(key: str) -> dict:
    """{found, rowNumber, docLink, emailStatus, emailSentAt, url} for a Source Key."""
    return _call("GET", "/api/community-refunds/handoff", query={"key": key})


def upsert(key: str, values: dict, approver: str, community_row: int) -> dict:
    """The hand-off row (the finance sheet's own column names → values)."""
    return _call("POST", "/api/community-refunds/handoff",
                 {"key": key, "values": values, "approver": approver, "communityRow": community_row})


def update(key: str, updates: dict, approver: str, why: str) -> dict:
    """The Doc link, the e-mail stamps — written back as they were on the sheet."""
    return _call("POST", "/api/community-refunds/handoff/update",
                 {"key": key, "updates": updates, "approver": approver, "why": why})
