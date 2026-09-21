# Community Ops app

A web app for the two things the Community Team does from the tracker
spreadsheet: **approving community refunds** and **replying to tickets**. It
runs on your PC for now (`http://localhost:8000`) and is built to be hosted
later for the whole team.

**Update, 21 Sep 2026 — the ticket tracker and Community Refund tab are being
retired.** The app now reads and writes its own SQLite database
(`data/ops.db`) for those two, not the Sheets API — see §3a. The finance
team's "Community Refunds Tracker" and the Google Form's `Refund_Clean`
landing sheet are unaffected and stay exactly as they are.

| | Sheet (Apps Script) | App |
|---|---|---|
| Learner's refund email | Zoho, from noreply@ | Same |
| Tracker row + Doc | As the trigger owner (Kawal) | As the signed-in approver |
| Finance email | From Kawal's Gmail, Reply-To approver | **From the approver's own Gmail** |
| Ticket reply | Zoho, from noreply@ | Same |
| Who can act | AUTHORIZED_SENDERS | AUTHORIZED_SENDERS + Google sign-in |

## 1. One-time setup (about 15 minutes)

### a. Google Cloud OAuth client

The app signs users in with Google and acts as them (Sheets, Drive, Gmail).
That needs an OAuth client:

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a
   project (or pick an existing one), e.g. "Community Ops".
2. **APIs & Services → Library**: enable **Google Sheets API**, **Google Drive
   API**, **Gmail API**.
3. **APIs & Services → OAuth consent screen**: User type **Internal** (only
   accounts in your Workspace can sign in — no Google verification needed).
   App name "Community Ops", your email as contact. Save.
   - If `addictivelearn.com` is a *separate* Workspace from `lawsikho.in`,
     Internal will not admit Baisakhi. Use **External** + add her as a test
     user, or keep the app in the Workspace that owns both domains.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   type **Web application**, name "Community Ops local", **Authorised redirect
   URI** exactly `http://localhost:8000/auth/callback`. Create. Copy the Client
   ID and Client secret.

### b. `.env`

```bash
python make_env.py
```

This creates `.env` from `.env.example`, generates `APP_SECRET`, and copies
the three `ZOHO_*` values from `../zoho-token-bridge/.env` (same Self Client
the bridge uses). Then open `.env` and paste in `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET`. Check `AUTHORIZED_SENDERS` lists everyone who should
be able to approve.

`.env` never leaves this machine. It is in `.gitignore`.

### c. Install and run

```bash
python -m pip install -r requirements.txt
```

(If `files.pythonhosted.org` will not resolve on your network, add
`-i https://pypi.tuna.tsinghua.edu.cn/simple`.)

Then double-click `run.bat`, or:

```bash
python -m uvicorn app.main:app --port 8000 --reload --reload-include .env
```

`run.bat` runs with auto-reload: any change to the code or to `.env` restarts
the server by itself. Keep the window open (minimise it) while the app is in
use — closing it stops the app. After Ctrl+C, Windows asks "Terminate batch
job (Y/N)?"; the server has already stopped by then, answer Y.

Open http://localhost:8000, sign in with your Google account, accept the
consent screen (Sheets, Drive, Gmail — the app needs all three), and go to
**Diagnostics**: it should show Zoho reachable, both spreadsheets opened by
name, and the Doc folder by name.

## 2. Using it

Both list pages show every field the old sheet columns held, still labelled
with the original column letter for anyone used to the sheet, 300 rows a
page, newest first, with search and filters. A second scrollbar above the
table stays pinned while you scroll down; headers stay fixed. Choosing
**Yes** in a Trigger dropdown sends immediately (after a confirm).

**Ticket replies** — every ticket in the database. Editable in the list:
**Resolution** (click, type, click away — saved with a ✓), **Resolution
status** (dropdown), **Trigger**. The row page edits course/requirement/
resolution/status/trigger with one **Save** button, which also sends when
Trigger is Yes and the row is unsent. **Send again** re-sends an
already-replied row, stacking a new timestamp.

**Refunds** — every refund request in the database. Editable: **Group**
(feeds the learner email and the derived Brand) and **Trigger**. Yes does, in
order: Zoho ticket + learner email + close, then tracker row + Doc + finance
email **from your Gmail** (still against the finance team's sheet — see §3a).
The row page shows both emails exactly as they will go out, and **Retry team
handoff** for a partial failure — it only does what is still missing.

**Diagnostics** — connectivity and configuration (reads only): Zoho
reachability, a row/refund count from the app's database, and the finance
spreadsheet and Doc folder opened by name.

Trigger/status dropdown options are fixed in the app (`store.py`) rather than
read from a sheet's validation rule now that there's no sheet to read one
from.

## 3. Where the rules live

`app/config.py` holds the column maps and the email templates, copied from
`00_Config.gs` and `RF_00_Config.gs`. **Script Properties are not readable by
the app**, so anything overridden there must be mirrored in `.env` (`RF_TEAM_TO`,
`RF_TEAM_CC`, and so on). If the sheet's layout ever changes, both the .gs
files and `config.py` change together — see HANDOVER.txt §3.

## 3a. Replacing the sheets — in progress (started 21 Sep 2026)

The app was built so the tracker spreadsheet's two automation tabs could be
retired without a rewrite: the workflows (`refunds.py`, `replies.py`) and
integrations (`zoho.py`, `google.py`) never touched the tracker spreadsheet
directly — only `sheets.py` did. That module is now `legacy_sheets.py`
(kept only for the one-off migration, not imported by the running app), and
`store.py` is its SQLite-backed replacement, same function shapes, `data/ops.db`
instead of the Sheets API.

Status:
- [x] `store.py` + `db.py` — the app is fully DB-backed for the ticket
  tracker and Community Refund tab. No Sheets calls happen for either any
  more.
- [x] One-off migration (`migrate.py`, `/diagnostics` → "Migrate sheet rows
  into the database") of the historical rows out of the two sheets into
  `data/ops.db`, preserving every workflow-state field so nothing double-sends.
- [x] Ported the ticket-sync/extraction/categorisation pipeline and the
  Refund_Clean intake — `zoho_sync.py` (port of `01_Main.gs` +
  `03_ZohoApi.gs`'s discovery/detail), `extraction.py` (`04_Extraction.gs`,
  minus image/OCR — see its docstring), `revenue.py` (`05_RevenueApi.gs`),
  `categorize.py` (`14_Categorize.gs`), `course_master.py` (ETL mirror of the
  Course Master sheet), `refund_intake.py` (`RF_01_Sync.gs`), all wired into
  `scheduler.py` (APScheduler, in-process, same cadence as the Apps Script
  triggers). `/diagnostics` has manual "run now" buttons for each job plus
  sync-cursor visibility, for testing ahead of the schedule.
- [ ] Needs before it can run for real: `ANTHROPIC_API_KEY` in `.env` (not
  currently set anywhere — see HANDOVER.txt), and `COURSE_SHEET_TAB` set
  explicitly (the Course Master spreadsheet's *first* tab, which the empty
  default falls back to, turned out to be an unrelated/broken tab — checked
  21 Sep 2026, see `course_master.py`).
- [ ] Run the new pipeline alongside Apps Script for a short verification
  window, then disable the Apps Script triggers (code stays, dormant).

`Refund_Clean` (the Google Form's landing sheet) and the finance team's
"Community Refunds Tracker" stay Google Sheets regardless — see HANDOVER.txt
§6.

## 4. Going live later

- Host it **anywhere but Google Cloud**: Zoho blocks Google's egress IPs
  (HANDOVER.txt §4), which is the reason the token bridge exists. A small
  VPS, Render, Railway or Fly box is fine.
- Set `APP_BASE_URL` to the public https URL, add the matching redirect URI
  (`https://…/auth/callback`) to the OAuth client, and set a fresh `APP_SECRET`.
- Copy `.env` and `data/tokens.db` is *not* needed — everyone signs in again.
- Keep the Apps Script automation running until the team has moved over; the
  two coexist safely.

## 5. Files

| File | What |
|---|---|
| `app/main.py` | Routes and pages |
| `app/config.py` | Settings, column maps, templates |
| `app/google.py` | Google sign-in; Sheets / Drive / Gmail as the user (still used for the finance-team handoff) |
| `app/zoho.py` | Zoho token + Desk calls (create, sendReply, close, search, conversations) |
| `app/db.py` | The app's own SQLite database — connection + schema |
| `app/store.py` | Ticket tracker + Community Refund rows, backed by `data/ops.db` |
| `app/legacy_sheets.py` | The old Sheets-backed reader — not imported; kept for the one-off migration only |
| `app/migrate.py` | One-off: copies the historical sheet rows into `data/ops.db` |
| `app/refunds.py` | Refund approval + team handoff (port of RF_02 + RF_04) |
| `app/replies.py` | Ticket reply + close (port of 09_ZohoReply) |
| `app/zoho_sync.py` | Ticket discovery, extraction pipeline, status/owner refresh (port of `01_Main.gs` + `03_ZohoApi.gs`) |
| `app/extraction.py` | Deterministic extraction + Claude fallback (port of `04_Extraction.gs`, text only) |
| `app/revenue.py` | Revenue System lookup adapter (port of `05_RevenueApi.gs`) |
| `app/categorize.py` | Gemini categorisation (port of `14_Categorize.gs`) |
| `app/course_master.py` | ETL mirror of the external Course Master sheet |
| `app/refund_intake.py` | Mirrors `Refund_Clean` into the `refunds` table (port of `RF_01_Sync.gs`) |
| `app/scheduler.py` | APScheduler wiring — runs the above on the same cadence Apps Script used |
| `app/templates/` | Pages |
| `make_env.py` | Builds `.env` without pasting secrets |
| `data/tokens.db` | Sign-in tokens (created on first login; never commit) |
| `data/ops.db` | Tickets + refunds (created on first run; never commit) |
