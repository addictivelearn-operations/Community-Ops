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
| Who can act | AUTHORIZED_SENDERS | AUTHORIZED_SENDERS (anyone on an ALLOWED_DOMAINS account can sign in and view — see §1) |

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
`GOOGLE_CLIENT_SECRET`.

`.env` never leaves this machine. It is in `.gitignore`.

**Two access tiers, added 21 Sep 2026** — `ALLOWED_DOMAINS` is who can sign
in at all (view-only by default: see every ticket/refund, nothing editable).
`AUTHORIZED_SENDERS` is the narrower list who can additionally change
anything (inline edits, trigger/send/approve, and the diagnostics admin
buttons) — enforced server-side (`main.py`'s `require_editor`), not just
hidden in the UI. Keep both in sync with whatever's actually deployed
(Railway or wherever) — they're separate copies, this file's changes don't
propagate there automatically.

**A third, narrower tier — `SUPERUSER_EMAIL`, added 21 Sep 2026** — the one
account (default `kawal@lawsikho.in`) that can add a manually-created test
row on the Refunds or Ticket replies page (`/refunds/new`, `/replies/new`)
and delete any row (`POST .../{row}/delete`) — enforced server-side
(`main.py`'s `require_superuser`), invisible to everyone else including
other `AUTHORIZED_SENDERS`. A test ticket's number is always forced to start
with `TEST-` (auto-generated if left blank) so it can never collide with a
real Zoho ticket number or get picked up by the Zoho sync. Delete removes
only the local DB row — it does **not** undo a Zoho email, tracker hand-off,
Doc or finance email that row already triggered; those have to be cleaned up
by hand if the row got that far before being deleted.

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
**Send again** (22 Sep 2026) re-sends the learner email on an already-sent
row — a brand new Zoho ticket, exactly like the first send, stacking a new
timestamp on top of the old one. It never touches the team handoff, which
keeps its own separate Retry button and its own Source Key idempotency.
**Force resend team handoff** (22 Sep 2026, superuser only) sends the
finance email again even when the tracker already has that Source Key
marked Sent — unlike Retry, which only fills what's missing and always
skips a row already marked Sent. For retesting a row with old tracker
history (a stale test artefact, or after changing `RF_TEAM_TO`/`RF_TEAM_CC`
to verify the new recipients) or fixing a genuinely wrong send. Real risk:
misused on a real row, it emails the finance team a second time.

**Read-only export** (21 Sep 2026) — `GET /api/refunds.json` returns every
refund row as JSON for the Master Refund Tracker's "Community Refunds" tab.
It needs the `X-Api-Key` header equal to `EXPORT_API_KEY` (set it on Railway;
empty = off). It reads what the Refunds page reads and writes nothing.

**Finance hand-off → the Master Refund Tracker app** (21 Sep 2026) — when
`TRACKER_URL` and `TRACKER_API_KEY` are set, Trigger = Yes sends the finance
row (the sheet's own 28 columns, as they were) to the tracker's Phase 4 →
"Community Refund" tab instead of the Community Refunds Tracker sheet; the
Doc link and the e-mail stamps are written back there too. The Zoho email,
the Doc and the finance email are unchanged. Unset = the sheet, as before.

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
- [x] **Ticket discovery was silently skipping tickets (fixed 22 Sep 2026)** —
  `fetch_community_tickets()` paged Zoho's ticket list sorted by
  `-modifiedTime` and stopped as soon as it saw one ticket older than its
  cutoff, trusting that sort to mean everything after it was older too. In
  this org `modifiedTime` is null for essentially every ticket, so that sort
  returns tickets in an order that has nothing to do with recency — verified
  live: a `-modifiedTime` page spanned 24 Aug–20 Sep while same-day tickets
  from minutes earlier never appeared in the first ten pages at all. Because
  `run()` advances `LAST_SYNC_ISO` even when it finds 0 candidates, a ticket
  the scan happened to miss this way was gone for good — no future sync ever
  looks at it again (only `refresh_statuses()` revisits tickets already in
  the database, it doesn't discover new ones). Fixed by sorting on
  `-createdTime` instead, which Zoho does populate and does sort correctly
  (verified live, strictly descending second-by-second) — and comparing the
  cutoff against `createdTime` specifically rather than the old
  `modifiedTime`-or-`createdTime` fallback, so the sort key and the
  comparison key always agree. `refresh_statuses()` (status/owner refresh
  for tickets already imported) still sorts on `-modifiedTime` and likely
  has the same unreliability for catching status changes on older tickets —
  not fixed here, since swapping it to `-createdTime` the same way would be
  wrong (a ticket's createdTime never changes, so it wouldn't help find
  which older tickets just changed status); it needs its own fix.
  **The fix only stops future misses — it doesn't recover past ones**: any
  ticket the bug already caused `LAST_SYNC_ISO` to advance past is
  permanently behind the cursor, since an ordinary sync only ever looks
  forward from it. `/diagnostics` → "Backfill missed tickets" rewinds the
  cursor by N days (default 3, superuser only) and runs the sync
  immediately — safe to run more than once, or with a wide N: discovery is
  still capped at `MAX_LIST_PAGES` tickets per call and every candidate is
  deduplicated by ticket number regardless of how far back the cursor
  points, so nothing already in the database gets touched twice.
- [x] **Reassignment sweep (22 Sep 2026)** — an OLD ticket transferred into
  Community Team from another department was still invisible to both fixes
  above: their only signal is `createdTime`, which doesn't change on
  reassignment. Kawal's call: the escalation risk of missing one outweighs
  the extra API cost, so build it. `zoho_sync.fetch_reassigned_tickets()`
  is a full scan of `/tickets/search?assigneeId=<Community Team's agent
  id>` (confirmed live — the plain `/tickets` LIST endpoint rejects
  `assigneeId`, search accepts it), which returns tickets by CURRENT
  assignment, independent of any timestamp. The agent id itself is resolved
  by name out of `agent_map()` (`zoho.team_agent_id()`), not hardcoded. No
  cutoff here on purpose — completeness, not recency, is the point — just
  `REASSIGN_MAX_LIST_PAGES` (default 30, currently ~8 pages/784 tickets for
  real) as a safety cap. Runs once a day at 04:00 (`scheduler.py`), not on
  the main 3x/day schedule, since a full scan costs more per run than the
  incremental sync's small window — also a manual "Run reassignment sweep
  now" on `/diagnostics`. `run()` and `run_reassignment_sweep()` now share
  `_process_batch()` (extract, insert, sync_log — the part that was
  identical either way) so the two entry points differ only in how they
  find candidates, not in what happens once they have them.
- [x] **Found and fixed while testing the sweep above: batches were mostly
  failing "database is locked"** — a real, pre-existing bug in
  `_process_batch()` (inherited as-is from the original `run()`, not
  introduced by the refactor), just not exposed until something processed
  many previously-unseen tickets in one go the way the sweep does.
  `_process_batch()` wrapped its WHOLE loop (up to 15 tickets) in one `with
  get_conn() as c:` — Python's sqlite3 module doesn't commit until that
  block exits, so the first ticket's `INSERT` left an uncommitted write
  transaction open for the rest of the batch, which can run a minute or
  more (each ticket does several slow network calls). `process_ticket()`
  meanwhile opens its OWN separate connections for cache reads/writes
  (`_lookup_learner_cache`, `_upsert_ai_cache`, ...), and every one of
  those collided with that long-held lock once contention ran past the
  10s busy-timeout. Fixed by giving each ticket's write its own
  connection, opened AFTER `process_ticket()` returns and committed
  immediately — nothing is ever held open across a network call anymore.
  Verified against the real Zoho org: the exact same 15-ticket batch went
  from 14/15 failing (all "database is locked") to 14/15 succeeding (the
  1 failure was `Gemini error: high demand`, unrelated). A second retry
  round hit the Gemini **free-tier daily quota** (20 requests/day/key)
  from the sheer volume of same-day testing — expected, not a bug, and
  already handled: a ticket that fails for any reason is simply never
  inserted, so it stays a candidate and gets picked up on the next run.
  This bug lived in code every ordinary `run()` call has always used too,
  so it was very likely a real, if less severe, contributor to tickets
  going missing before today — not just the sort-order bug above.
- [x] **Resync un-sent refunds from Refund_Clean (23 Sep 2026)** — a
  Refund_Clean bug (since fixed at the source) had put the course name in
  the Reason column for some rows; those already synced into the database
  with the bad data, and `refund_intake.run()` can't fix that — it only
  ever inserts a Source Key it hasn't seen before, never revisits one
  already there. `refund_intake.resync_unsent()` re-reads Refund_Clean A-J
  and, for every LOCAL row matched by the same Source Key (timestamp +
  email) that has NOT been sent yet, overwrites name/phone/group/reason/
  funnel/funnel_final/community/amount with the sheet's current values. A
  row already emailed is never touched, no matter what — Kawal's explicit
  call, to keep the historical record of what was actually sent intact.
  `POST /admin/sync/refunds/resync-unsent` + a Diagnostics button, same
  `require_editor` tier as the other sync buttons (in practice
  superuser-only in the UI, since only Kawal can see `/diagnostics` at
  all — see the access-tiers section). Verified against real data: cloned
  a real sent row's Source Key onto a temporary un-sent test row with
  deliberately wrong reason/community, ran the resync, confirmed the real
  sent row was completely untouched while the test row's data was pulled
  back to match the sheet exactly, then deleted the test row.
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
