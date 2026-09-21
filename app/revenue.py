"""
Revenue System API fallback lookup — port of 05_RevenueApi.gs. Called ONLY
when learner name/phone could not be confidently determined from Zoho Desk
data (and the learner cache had no answer), to minimise API usage.

ADAPTER: the request shape and response parsing are a sensible default
(POST {"student_email": ...} with a Bearer token). Adjust _build_request and
_parse_response to match the Revenue System's actual wire format if it
differs — they are the only two places that know about it.
"""

import httpx

from .config import settings
from .extraction import normalise_phone


def _build_request(email: str) -> tuple[str, dict]:
    is_post = settings.revenue_http_method.lower() != "get"
    url = settings.revenue_api_url
    headers: dict = {}
    params: dict = {}

    if not is_post:
        params["email"] = email

    if settings.revenue_api_key:
        style = settings.revenue_auth_style.lower()
        if style == "x-api-key":
            headers["X-API-Key"] = settings.revenue_api_key
        elif style == "query":
            params["api_key"] = settings.revenue_api_key
        else:  # bearer
            headers["Authorization"] = f"Bearer {settings.revenue_api_key}"

    kwargs = {"headers": headers, "params": params, "timeout": 30}
    if is_post:
        headers["Accept"] = "application/json"
        kwargs["json"] = {"student_email": email}
    return ("POST" if is_post else "GET", url, kwargs)


def _parse_response(body) -> dict | None:
    rec = body
    if isinstance(rec, dict) and rec.get("data") is not None:
        rec = rec["data"]
    if isinstance(rec, list):
        rec = rec[0] if rec else None
    if not isinstance(rec, dict):
        return None

    name = (rec.get("name") or rec.get("student_name") or rec.get("learner_name") or
           rec.get("full_name") or f"{rec.get('first_name', '')} {rec.get('last_name', '')}".strip())
    phone = (rec.get("phone") or rec.get("student_phone") or rec.get("student_mobile") or
            rec.get("mobile") or rec.get("phone_number") or rec.get("contact_number") or
            rec.get("registered_phone") or "")
    course = (rec.get("course") or rec.get("course_name") or rec.get("student_course") or
             rec.get("enrolled_course") or rec.get("purchased_course") or rec.get("program") or
             rec.get("product_name") or "")

    if not name and not phone and not course:
        return None
    return {"name": str(name).strip(), "phone": normalise_phone(str(phone)), "course": str(course).strip()}


def fetch_revenue_data(email: str) -> dict | None:
    """{'name', 'phone', 'course'} or None when unavailable/failed/not found.
    Never raises — a Revenue failure must never block a ticket import."""
    if not settings.revenue_api_enabled or not settings.revenue_api_url or not email:
        return None
    try:
        method, url, kwargs = _build_request(email)
        r = httpx.request(method, url, **kwargs)
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            return None
        return _parse_response(r.json())
    except Exception:  # noqa: BLE001 — degrade to "no data", never raise
        return None
