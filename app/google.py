"""
Google: sign-in (OAuth 2.0) and the APIs the app uses AS THE SIGNED-IN USER —
Sheets, Drive (Doc creation + sharing) and Gmail (the finance email).

Acting as the user, not as a service account, is deliberate:
  • sheet edits are attributed to the approver, exactly like editing by hand;
  • the Doc is owned by them, in their Drive folder;
  • the finance email leaves THEIR Gmail — which Apps Script could never do,
    because an installable trigger always runs as its installer.

Tokens are kept in a small SQLite file (data/tokens.db). Only the refresh
token is durable; access tokens are minted on demand and cached until expiry.
"""

import base64
import json
import secrets
import sqlite3
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from .config import ROOT, settings

SCOPES = [
    "openid", "email", "profile",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
]

DB_PATH = ROOT / "data" / "tokens.db"
HTTP_TIMEOUT = 30


class GoogleError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, name TEXT, "
        "refresh_token TEXT, access_token TEXT, expires_at REAL)"
    )
    return con


def save_user(email: str, name: str, refresh_token: str | None,
              access_token: str, expires_in: int) -> None:
    with _db() as con:
        if refresh_token is None:
            # Google omits the refresh token on re-consent; keep the old one.
            row = con.execute("SELECT refresh_token FROM users WHERE email=?",
                              (email,)).fetchone()
            refresh_token = row[0] if row else None
        con.execute(
            "INSERT OR REPLACE INTO users VALUES (?,?,?,?,?)",
            (email, name, refresh_token, access_token, time.time() + expires_in - 60),
        )


def forget_user(email: str) -> None:
    with _db() as con:
        con.execute("DELETE FROM users WHERE email=?", (email,))


def _load_user(email: str):
    with _db() as con:
        return con.execute(
            "SELECT name, refresh_token, access_token, expires_at FROM users WHERE email=?",
            (email,)).fetchone()


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

def redirect_uri() -> str:
    return settings.base_url + "/auth/callback"


def auth_url(state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",          # always returns a refresh token
        "include_granted_scopes": "true",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def new_state() -> str:
    return secrets.token_urlsafe(24)


def exchange_code(code: str) -> dict:
    """Code → tokens + the signed-in identity (from the id_token)."""
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise GoogleError(f"Google token exchange failed: {r.text[:300]}")
    body = r.json()
    # The id_token came straight from Google's token endpoint over TLS, so its
    # payload is trusted without a second signature check.
    payload = body["id_token"].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    ident = json.loads(base64.urlsafe_b64decode(payload))
    return {
        "email": str(ident.get("email", "")).lower(),
        "name": ident.get("name") or ident.get("email", ""),
        "email_verified": bool(ident.get("email_verified")),
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_in": int(body.get("expires_in", 3600)),
    }


def is_allowed(email: str) -> tuple[bool, str]:
    """Sign-in eligibility only — domain membership. Being an AUTHORIZED_SENDER
    is a separate, narrower check (main.py's require_editor) for who may make
    changes; everyone on an allowed domain gets read-only access by signing
    in at all."""
    domain = email.rsplit("@", 1)[-1]
    if domain not in settings.allowed_domains:
        return False, f"{email} is not on an allowed domain ({', '.join(settings.allowed_domains)})."
    return True, ""


# ---------------------------------------------------------------------------
# API client, per user
# ---------------------------------------------------------------------------

class GoogleClient:
    def __init__(self, email: str):
        self.email = email
        row = _load_user(email)
        if not row:
            raise GoogleError("Not signed in — no Google token on file. Sign in again.")
        self.name, self._refresh, self._access, self._expires = row
        self.http = httpx.Client(timeout=HTTP_TIMEOUT)

    # --- auth ---------------------------------------------------------------

    def token(self) -> str:
        if self._access and time.time() < self._expires:
            return self._access
        if not self._refresh:
            raise GoogleError("No refresh token on file — sign out and sign in again.")
        r = self.http.post("https://oauth2.googleapis.com/token", data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": self._refresh,
            "grant_type": "refresh_token",
        })
        if r.status_code != 200:
            raise GoogleError(f"Google refresh failed (sign in again): {r.text[:300]}")
        body = r.json()
        self._access = body["access_token"]
        self._expires = time.time() + int(body.get("expires_in", 3600)) - 60
        save_user(self.email, self.name, None, self._access, int(body.get("expires_in", 3600)))
        return self._access

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def _call(self, method: str, url: str, **kw) -> dict | None:
        r = self.http.request(method, url, headers=self._headers(), **kw)
        if r.status_code >= 300:
            raise GoogleError(f"Google API {method} {url.split('?')[0]} failed "
                              f"(HTTP {r.status_code}): {r.text[:400]}")
        return r.json() if r.text else None

    # --- Sheets --------------------------------------------------------------

    @staticmethod
    def _a1(tab: str, rng: str) -> str:
        return quote(f"'{tab}'!{rng}", safe="!:'")

    def sheet_values(self, sheet_id: str, tab: str, rng: str) -> list[list]:
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
               f"{self._a1(tab, rng)}?valueRenderOption=UNFORMATTED_VALUE"
               f"&dateTimeRenderOption=SERIAL_NUMBER")
        body = self._call("GET", url) or {}
        return body.get("values", [])

    def sheet_write(self, sheet_id: str, tab: str, rng: str, values: list[list],
                    raw: bool = True) -> None:
        """RAW keeps strings as typed (a phone stays text); USER_ENTERED lets
        Sheets parse dates/numbers the way a person typing them would."""
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
               f"{self._a1(tab, rng)}?valueInputOption={'RAW' if raw else 'USER_ENTERED'}")
        self._call("PUT", url, json={"values": values})

    def sheet_write_cells(self, sheet_id: str, tab: str, cells: dict, raw: bool = True) -> None:
        """Several single cells in one request: {'K412': 'text', 'M412': 'Yes'}."""
        if not cells:
            return
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate"
        self._call("POST", url, json={
            "valueInputOption": "RAW" if raw else "USER_ENTERED",
            "data": [{"range": f"'{tab}'!{a1}", "values": [[v]]} for a1, v in cells.items()],
        })

    def validation_list(self, sheet_id: str, tab: str, a1: str) -> list[str] | None:
        """The dropdown options on one cell, read from the sheet's own data
        validation rule — so the app offers exactly what the sheet offers.
        None when the cell has no list validation."""
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
               f"?ranges={self._a1(tab, a1)}&fields=sheets(data(rowData(values(dataValidation))))")
        body = self._call("GET", url) or {}
        try:
            cond = body["sheets"][0]["data"][0]["rowData"][0]["values"][0]["dataValidation"]["condition"]
        except (KeyError, IndexError):
            return None
        vals = [v.get("userEnteredValue", "") for v in cond.get("values", [])]
        if cond.get("type") == "ONE_OF_LIST":
            return [v for v in vals if v != ""]
        if cond.get("type") == "ONE_OF_RANGE" and vals:
            ref = vals[0].lstrip("=")
            if "!" in ref:
                rtab, rng = ref.split("!", 1)
                rtab = rtab.strip("'")
            else:
                rtab, rng = tab, ref
            rows = self.sheet_values(sheet_id, rtab, rng)
            return [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]
        return None

    def sheet_tabs(self, sheet_id: str) -> list[dict]:
        body = self._call("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
                          "?fields=properties.title,sheets.properties") or {}
        return [s["properties"] for s in body.get("sheets", [])]

    def sheet_title(self, sheet_id: str) -> str:
        body = self._call("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
                          "?fields=properties.title") or {}
        return body.get("properties", {}).get("title", "")

    def sheet_add_tab(self, sheet_id: str, title: str) -> int:
        body = self._call("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate",
                          json={"requests": [{"addSheet": {"properties": {"title": title}}}]})
        return body["replies"][0]["addSheet"]["properties"]["sheetId"]

    def sheet_format(self, sheet_id: str, requests: list[dict]) -> None:
        self._call("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate",
                   json={"requests": requests})

    # --- Drive / Docs --------------------------------------------------------

    def create_doc_from_html(self, title: str, html: str, folder_id: str | None) -> dict:
        """Drive converts uploaded HTML into a Google Doc — tables included —
        which is far simpler than building the Doc through the Docs API."""
        meta = {"name": title, "mimeType": "application/vnd.google-apps.document"}
        if folder_id:
            meta["parents"] = [folder_id]
        boundary = "b" + secrets.token_hex(12)
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n"
            f"{html}\r\n--{boundary}--"
        ).encode("utf-8")
        r = self.http.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
            "&fields=id,webViewLink&supportsAllDrives=true",
            headers={**self._headers(), "Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        )
        if r.status_code >= 300:
            raise GoogleError(f"Doc creation failed (HTTP {r.status_code}): {r.text[:400]}")
        return r.json()

    def share_anyone_view(self, file_id: str) -> str:
        try:
            self._call("POST", f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions"
                       "?supportsAllDrives=true", json={"role": "reader", "type": "anyone"})
            return ""
        except GoogleError:
            try:
                self._call("POST", f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions"
                           "?supportsAllDrives=true",
                           json={"role": "reader", "type": "domain",
                                 "domain": self.email.rsplit("@", 1)[-1]})
                return ""
            except GoogleError as e:
                return f"could not set link sharing: {e}"

    def folder_name(self, folder_id: str) -> str:
        body = self._call("GET", f"https://www.googleapis.com/drive/v3/files/{folder_id}"
                          "?fields=name&supportsAllDrives=true") or {}
        return body.get("name", "")

    # --- Gmail ---------------------------------------------------------------

    def send_mail(self, *, from_name: str, to: list[str], cc: list[str], bcc: list[str],
                  subject: str, html: str, reply_to: str | None = None) -> str:
        msg = EmailMessage()
        msg["From"] = formataddr((from_name, self.email))
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        if reply_to:
            msg["Reply-To"] = reply_to
        msg["Subject"] = subject
        msg.set_content("This email needs an HTML-capable client.")
        msg.add_alternative(html, subtype="html")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body = self._call("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                          json={"raw": raw}) or {}
        return body.get("id", "")
