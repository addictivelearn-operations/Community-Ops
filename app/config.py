"""
Settings and the column maps.

Everything here mirrors the Apps Script project's 00_Config.gs / RF_00_Config.gs
so that a row approved in this app is indistinguishable from one approved in
the sheet. The Apps Script Script Properties are NOT readable from here — any
value overridden there (recipients, templates) must be overridden in .env too.

Column numbers are 1-based, like the .gs files, and NEVER change unless the
sheet's layout does. See HANDOVER.txt section 3 before touching them.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return default if v is None or v == "" else v.strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


def _json(key: str, default):
    raw = _get(key, "")
    if not raw:
        return default
    return json.loads(raw)


def _csv(key: str, default: str = "") -> list[str]:
    return [p.strip() for p in _get(key, default).split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Main tab — "From 27th May 26 onward" (ticket replies)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MainCols:
    TICKET = 1        # A  SME Support Request ID (ticket NUMBER)
    OWNER = 2         # B  ticket owner
    TIMESTAMP = 3     # C  ticket created (Zoho)
    IMPORTED_AT = 4   # D  when the row landed in the sheet
    BRAND = 5         # E
    NAME = 6          # F
    EMAIL = 7         # G
    PHONE = 8         # H
    COURSE = 9        # I
    REQUIREMENT = 10  # J
    RESOLUTION = 11   # K
    RES_STATUS = 12   # L
    TRIGGER = 13      # M  "Trigger Email" — Yes / Pending / NA
    SENT_AT = 14      # N  stacked stamps, newest first
    SENT_BY = 15      # O
    COURSE_WP = 16    # P
    CATEGORY = 17     # Q
    RESULT = 18       # R  outcome
    THREAD_LINK = 19  # S
    STATUS = 20       # T  Zoho status
    LAST = 20


# ---------------------------------------------------------------------------
# Refund tab — "Community Refund"
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RefundCols:
    TIMESTAMP = 1     # A
    NAME = 2          # B
    EMAIL = 3         # C
    PHONE = 4         # D
    GROUP = 5         # E
    REASON = 6        # F
    FUNNEL = 7        # G
    FUNNEL_FINAL = 8  # H  "No order found" selects the second template
    COMMUNITY = 9     # I
    AMOUNT = 10       # J
    BRAND = 11        # K  ARRAY FORMULA on the sheet — read only
    TRIGGER = 14      # N
    SENT_AT = 15      # O
    SENT_BY = 16      # P
    RESULT = 17       # Q
    HANDOFF = 18      # R
    LAST = 18


# ---------------------------------------------------------------------------
# Team tracker — "Community Refunds Tracker" (finance team's spreadsheet)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TeamCols:
    TIMESTAMP = 1
    APPROVER_EMAIL = 2
    BRAND = 3
    NAME = 4
    EMAIL = 5
    PHONE = 6
    PRODUCT = 7
    REASON = 8
    APPROVED_BY = 9
    AMOUNT = 10
    DOC_LINK = 16     # P
    MAIL_STATUS = 17  # Q
    MAIL_SENT_AT = 18 # R
    DEADLINE = 19     # S
    SOURCE_KEY = 28   # AB
    LAST = 28


# Word for word from "Non Access Revoke Refunds Tracker" row 1 (14 Sep 2026),
# typos included. AB "Source Key" is ours.
TEAM_HEADERS = [
    "Timestamp", "Email address", "Brand", "Payee Name", "Payee Email",
    "Payee Number", "Product (Course) Purchased", "Reason For Refund",
    "Refund Approved By (Update NA if an approval is not needed)",
    "Refund Amount (Enter the exact amount along with the proper currency sign)",
    "Bank Name - Leave this field blank if not required.",
    "Bank Account Holder Name - Leave this field blank if not required.",
    "Bank Account Number - Leave this field blank if not required.",
    "Bank IFSC Code - Leave this field blank if not required.",
    "Finance/Loan/Payment Partner - Leave this field blank if not required.",
    "Refund Approval Document", "Email Status", "Email Sent Timestamp",
    "Refund should Ideally Be Processed by Date?",
    "Actual Amount to be Refunded - INR\n(Finance Team)",
    "Refund Processed?\n(Finance Team)", "Column 22", "Column 23",
    "Comments (Finance Team)", "Student Inforrmed?", "Emrollment Details",
    "UTR (Finance Team)", "Source Key",
]


# ---------------------------------------------------------------------------
# Email templates (identical to the Apps Script defaults)
# ---------------------------------------------------------------------------

RF_SUBJECT = "Update Regarding Your Community Refund"
RF_SUBJECT_NO_ORDER = "Action Required: Unable to Trace Your Community Payment"
RF_NO_ORDER_VALUE = "No order found"
RF_CURRENCY = "₹"

RF_TEMPLATE = "\n\n".join([
    "Dear {{name}},",
    "Thank you for your patience.",
    "We would like to inform you that a refund of {{amount}} for your "
    "community {{community}} has been initiated, and your refund request "
    "has been raised with the concerned team for further review and "
    "processing.",
    "Please note that once the refund request is raised, you will be "
    "removed from the respective WhatsApp community.",
    "The refund process may take approximately 10–15 working days to be "
    "completed and credited to your account.",
    "We appreciate your patience and understanding throughout the process.",
    "Warm regards,\nLawSikho Team",
])

RF_TEMPLATE_NO_ORDER = "\n\n".join([
    "Hi {{name}},",
    "Thank you for reaching out to us.",
    "We would like to inform you that we are unable to trace your WhatsApp "
    "Community payment using the email ID you shared.",
    "We request you to kindly refill the form below using the *correct "
    "email ID that was used while making the payment*. Once the details "
    "match our payment records, your refund will be processed accordingly.",
    "Please make sure to fill in the form carefully:",
    "[Refill the form here](https://forms.gle/9HGoJz84fMmr6fEaA)",
    "Thank you for your cooperation.",
    "Warm regards,\nLawSikho Team",
])


# ---------------------------------------------------------------------------
# Settings object
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    app_secret: str = _get("APP_SECRET", "dev-secret-change-me")
    base_url: str = _get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(_get("APP_TIMEZONE", "Asia/Kolkata")))

    google_client_id: str = _get("GOOGLE_CLIENT_ID")
    google_client_secret: str = _get("GOOGLE_CLIENT_SECRET")
    allowed_domains: list[str] = field(default_factory=lambda: _csv("ALLOWED_DOMAINS", "lawsikho.in,addictivelearn.com"))
    authorized_senders: dict[str, str] = field(default_factory=lambda: {
        k.lower(): v for k, v in _json("AUTHORIZED_SENDERS", {
            "baisakhi@addictivelearn.com": "Baisakhi",
            "ayushi.s@lawsikho.in": "Ayushi",
        }).items()
    })
    # The only account that may add a test entry or delete any entry — a
    # narrower right than AUTHORIZED_SENDERS, which every editor is in
    # (21 Sep 2026).
    superuser_email: str = _get("SUPERUSER_EMAIL", "kawal@lawsikho.in").lower()

    zoho_client_id: str = _get("ZOHO_CLIENT_ID")
    zoho_client_secret: str = _get("ZOHO_CLIENT_SECRET")
    zoho_refresh_token: str = _get("ZOHO_REFRESH_TOKEN")
    zoho_org_id: str = _get("ZOHO_ORG_ID", "60013446135")
    zoho_dc: str = _get("ZOHO_DC", "in")

    tracker_sheet_id: str = _get("TRACKER_SHEET_ID", "1zVyWQKDkUIO0Naf97cmb64C4FJgzd1_m5c5TBTSEMO0")
    main_tab: str = _get("MAIN_TAB", "From 27th May 26 onward")
    main_first_row: int = 400
    refund_tab: str = _get("REFUND_TAB", "Community Refund")

    reply_default_from: str = _get("REPLY_DEFAULT_FROM", "noreply@addictivelearn.com")
    reply_from_addresses: dict[str, str] = field(default_factory=lambda: {
        k.lower(): v for k, v in _json("REPLY_FROM_ADDRESSES", {}).items()
    })
    reply_close_status: str = _get("REPLY_CLOSE_STATUS", "Closed")

    rf_department_id: str = _get("RF_DEPARTMENT_ID", "67898000253072456")
    rf_from_address: str = _get("RF_FROM_ADDRESS", "noreply@addictivelearn.com")

    team_sheet_id: str = _get("RF_TEAM_SHEET_ID", "14CjWJ0Q5N57Dwxtg8IraK54jeppYbxTRfugDz5K2esA")
    team_tab: str = _get("RF_TEAM_TAB", "Community Refunds Tracker")
    team_doc_folder_id: str = _get("RF_TEAM_DOC_FOLDER_ID", "1CMC8YphXc6wd2w5CPgmVNNDS3sLur8lo")
    team_first_auto_row: int = _int("RF_TEAM_FIRST_AUTO_ROW", 51)
    team_to: list[str] = field(default_factory=lambda: _csv("RF_TEAM_TO"))
    team_cc: list[str] = field(default_factory=lambda: _csv("RF_TEAM_CC"))
    team_bcc: list[str] = field(default_factory=lambda: _csv("RF_TEAM_BCC", "internal_comm@lawsikho1.zohodesk.in"))
    team_signature: str = _get("RF_TEAM_SIGNATURE", "Community Team")
    team_deadline_days: int = _int("RF_TEAM_DEADLINE_DAYS", 15)

    # --- Read-only export for the Master Refund Tracker (21 Sep 2026) --------
    # GET /api/refunds.json with header X-Api-Key equal to this value returns
    # every refund row as JSON. Empty = the endpoint is switched off (503).
    export_api_key: str = _get("EXPORT_API_KEY", "")
    # --- The Master Refund Tracker app (21 Sep 2026) --------------------------
    # Where the finance hand-off row goes instead of the "Community Refunds
    # Tracker" sheet: POST {TRACKER_URL}/api/community-refunds/handoff with
    # X-Api-Key = TRACKER_API_KEY. Both empty = the sheet, exactly as before.
    tracker_url: str = _get("TRACKER_URL", "").rstrip("/")
    tracker_api_key: str = _get("TRACKER_API_KEY", "")
    team_doc_title_prefix: str = "Community Refund Approval Document - "

    # --- Ticket sync (port of zoho-desk-sync) --------------------------------
    team_name: str = _get("TEAM_NAME", "Community Team")
    allowed_departments: list[str] = field(default_factory=lambda: _json(
        "ALLOWED_DEPARTMENTS", ["LawSikho", "SKILLARBITRAGE", "Skill Arbitrage"]))
    sync_since_iso: str = _get("SYNC_SINCE_ISO", "2026-07-01T00:00:00.000Z")
    sync_overlap_minutes: int = _int("SYNC_OVERLAP_MINUTES", 5)
    max_tickets_per_run: int = _int("BATCH_SIZE", 15)
    max_list_pages: int = _int("MAX_LIST_PAGES", 10)
    # The daily reassignment sweep (fetch_reassigned_tickets, 22 Sep 2026) is
    # a full scan, not a windowed one — currently ~8 pages/784 tickets for
    # Community Team, grows slowly over time. 30 pages (3,000 tickets) is
    # headroom, not an expected size.
    reassign_max_list_pages: int = _int("REASSIGN_MAX_LIST_PAGES", 30)
    tickets_per_pause: int = _int("TICKETS_PER_PAUSE", 100)
    pause_ms: int = _int("PAUSE_MS", 2000)
    max_conversations: int = 50
    max_text_chars_per_thread: int = 6000
    max_total_text_chars: int = 40000
    schedule_times: list[list[int]] = field(default_factory=lambda: _json(
        "SCHEDULE_TIMES", [[10, 15], [15, 0], [18, 0]]))

    # --- Status refresh -------------------------------------------------------
    status_overlap_minutes: int = _int("STATUS_OVERLAP_MINUTES", 15)
    status_max_list_pages: int = _int("STATUS_MAX_LIST_PAGES", 20)
    status_first_run_lookback_hours: int = _int("STATUS_FIRST_RUN_LOOKBACK_HOURS", 24)

    # --- Learner / AI caches ---------------------------------------------------
    learner_cache_ttl_days: int = _int("LEARNER_CACHE_TTL_DAYS", 90)
    ai_cache_ttl_days: int = _int("AI_CACHE_TTL_DAYS", 30)

    # --- Trivial-ticket deterministic summary ----------------------------------
    trivial_summary_enabled: bool = _get("TRIVIAL_SUMMARY_ENABLED", "true").lower() == "true"
    trivial_max_messages: int = _int("TRIVIAL_MAX_MESSAGES", 1)
    trivial_max_chars: int = _int("TRIVIAL_MAX_CHARS", 600)

    # --- AI extraction fallback + summary --------------------------------------
    # Runs on Gemini (gemini_api_key below, extraction.py) — Kawal's choice,
    # 21 Sep 2026, over adding a new ANTHROPIC_API_KEY. The Anthropic settings
    # are kept, unused, in case that changes; nothing reads them right now.
    ai_enabled: bool = _get("AI_ENABLED", "true").lower() == "true"
    ai_extraction_enabled: bool = _get("AI_EXTRACTION_ENABLED", "true").lower() == "true"
    ai_summary_enabled: bool = _get("AI_SUMMARY_ENABLED", "true").lower() == "true"
    anthropic_api_key: str = _get("ANTHROPIC_API_KEY", "")
    claude_model: str = _get("CLAUDE_MODEL", "claude-opus-4-8")
    claude_max_tokens: int = 8192

    # --- Revenue System fallback ------------------------------------------------
    revenue_api_enabled: bool = _get("REVENUE_API_ENABLED", "true").lower() == "true"
    revenue_api_url: str = _get("REVENUE_API_URL", "")
    revenue_api_key: str = _get("REVENUE_API_KEY", "")
    revenue_auth_style: str = _get("REVENUE_AUTH_STYLE", "bearer")
    revenue_http_method: str = _get("REVENUE_HTTP_METHOD", "post")

    # --- Course master sheet (external, read-only) ------------------------------
    course_sheet_enabled: bool = _get("COURSE_SHEET_ENABLED", "true").lower() == "true"
    course_sheet_id: str = _get("COURSE_SHEET_ID", "1RfEkM3x2ll-PXUc6CK7bwZujI2Vhj3yKTbA1T8HSVi4")
    course_sheet_tab: str = _get("COURSE_SHEET_TAB", "")  # '' = first tab
    course_sheet_name_col: int = _int("COURSE_SHEET_NAME_COL", 4)
    course_sheet_email_col: int = _int("COURSE_SHEET_EMAIL_COL", 5)
    course_sheet_phone_col: int = _int("COURSE_SHEET_PHONE_COL", 6)
    course_sheet_course_col: int = _int("COURSE_SHEET_COURSE_COL", 12)
    course_aliases: dict[str, str] = field(default_factory=lambda: _json("COURSE_ALIASES", {}))

    # --- Categorisation (Gemini) -------------------------------------------------
    gemini_api_key: str = _get("GEMINI_API_KEY", "")
    gemini_model: str = _get("GEMINI_MODEL", "gemini-3.6-flash")
    category_max_words: int = _int("CATEGORY_MAX_WORDS", 5)
    category_keyword_rules: list = field(default_factory=lambda: _json(
        "CATEGORY_KEYWORD_RULES", [["boot camp", "Bootcamp Query"], ["class", "Class Schedule Query"]]))
    category_aliases: dict[str, str] = field(default_factory=lambda: _json("CATEGORY_ALIASES", {
        "WhatsApp Group Addition Request": "Whatsapp group addition/access",
        "WhatsApp Group Access Issue": "Whatsapp group addition/access",
        "Joining-related queries": "Whatsapp group addition/access",
        "Community Addition Request": "Whatsapp group addition/access",
        "Community Access Query": "Whatsapp group addition/access",
        "Whatsaap group addition/access.": "Whatsapp group addition/access",
        "Whatsapp group addition/access.": "Whatsapp group addition/access",
        "webinar/ session related queries": "Class Schedule Query",
        "Course Onboarding Inquiry": "Course Onboarding Query",
        "certificate-related": "Certificate Access Issue",
        "Uncategorised": "Not Enough Information",
        "Uncategorized": "Not Enough Information",
        "Others": "Not Enough Information",
        "Other": "Not Enough Information",
    }))
    category_empty_label: str = _get("CATEGORY_EMPTY_LABEL", "Not Enough Information")
    category_batch_size: int = _int("CATEGORY_BATCH_SIZE", 40)
    category_max_requests_per_sync: int = _int("CATEGORY_MAX_REQUESTS_PER_SYNC", 2)
    category_pause_ms: int = _int("CATEGORY_PAUSE_MS", 4000)
    category_time_budget_seconds: int = _int("CATEGORY_TIME_BUDGET_SECONDS", 270)
    category_max_existing_labels: int = _int("CATEGORY_MAX_EXISTING_LABELS", 60)

    # --- Refund intake (port of RF_01_Sync.gs) -----------------------------------
    rf_source_sheet_id: str = _get("RF_SOURCE_SHEET_ID", "1WfKaoIbJGMMLBsHq981DrW85Iz1W1Q7gfeSdqwX8xhU")
    rf_source_tab: str = _get("RF_SOURCE_TAB", "Refund_Clean")
    rf_since_iso: str = _get("RF_SINCE_ISO", "2026-09-01T00:00:00")
    rf_sync_interval_minutes: int = _int("RF_SYNC_INTERVAL_MINUTES", 15)

    # --- Unattended background jobs ------------------------------------------
    # Whose stored Google token the scheduler borrows to read the two external,
    # read-only sheets (Course Master, Refund_Clean) with nobody signed in.
    # That person must have logged into this app at least once already.
    sync_service_account_email: str = _get("SYNC_SERVICE_ACCOUNT_EMAIL", "kawal@lawsikho.in")

    @property
    def zoho_accounts(self) -> str:
        return f"https://accounts.zoho.{self.zoho_dc}"

    @property
    def zoho_desk_base(self) -> str:
        return f"https://desk.zoho.{self.zoho_dc}/api/v1"

    def missing(self) -> list[str]:
        """Settings without which the app cannot start."""
        out = []
        if not self.google_client_id or not self.google_client_secret:
            out.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")
        if not (self.zoho_client_id and self.zoho_client_secret and self.zoho_refresh_token):
            out.append("ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN")
        if self.app_secret == "dev-secret-change-me":
            out.append("APP_SECRET (still the placeholder)")
        return out


settings = Settings()
MAIN = MainCols()
RF = RefundCols()
TEAM = TeamCols()
