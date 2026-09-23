"""
In-process scheduler — replaces the Apps Script time-driven triggers
(13_Schedule.gs, RF_01_Sync.gs's installRefundFormTrigger). Runs only while
this app's process is up, same constraint the token bridge already
documents in HANDOVER.txt §4.

Jobs, matching today's Apps Script cadence:
  - ticket sync + categorisation + status refresh, at settings.schedule_times
    (default 10:15 / 15:00 / 18:00, matching the live Apps Script schedule)
  - refund intake (Refund_Clean -> refunds table), every
    settings.rf_sync_interval_minutes (default 15)
  - course master ETL, once a day (a slow-moving reference dataset)
  - reassignment sweep (22 Sep 2026), once a day at 04:00 — the ticket sync
    above only ever finds tickets by createdTime, so it can't see one
    reassigned into Community Team from another department after the fact;
    this is a full Zoho assignee-search scan that can. Deliberately once a
    day, not on the main schedule: it costs meaningfully more API calls per
    run than the incremental sync (a full scan, not a small window), and
    this org has hit Zoho's daily cap before (HANDOVER.txt §5).
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import categorize, course_master, refund_intake, zoho_sync
from .config import settings

log = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None


def _run_full_sync() -> None:
    try:
        result = zoho_sync.run()
        log.info("zoho_sync.run(): %s", result)
    except Exception:  # noqa: BLE001 — one job failing must not kill the scheduler
        log.exception("zoho_sync.run() failed")
    try:
        result = categorize.categorize_new_rows(settings.category_max_requests_per_sync)
        log.info("categorize_new_rows(): %s", result)
    except Exception:  # noqa: BLE001
        log.exception("categorize_new_rows() failed")
    try:
        result = zoho_sync.refresh_statuses()
        log.info("refresh_statuses(): %s", result)
    except Exception:  # noqa: BLE001
        log.exception("refresh_statuses() failed")


def _run_reassignment_sweep() -> None:
    try:
        result = zoho_sync.run_reassignment_sweep()
        log.info("run_reassignment_sweep(): %s", result)
    except Exception:  # noqa: BLE001
        log.exception("run_reassignment_sweep() failed")


def _run_refund_intake() -> None:
    try:
        result = refund_intake.run()
        log.info("refund_intake.run(): %s", result)
    except Exception:  # noqa: BLE001
        log.exception("refund_intake.run() failed")


def _run_course_master() -> None:
    try:
        result = course_master.refresh()
        log.info("course_master.refresh(): %s", result)
    except Exception:  # noqa: BLE001
        log.exception("course_master.refresh() failed")


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=settings.tz)
    now = datetime.now(settings.tz)

    for hour, minute in settings.schedule_times:
        _scheduler.add_job(_run_full_sync, CronTrigger(hour=hour, minute=minute, timezone=settings.tz),
                           id=f"full_sync_{hour:02d}{minute:02d}", replace_existing=True)

    # Interval jobs with no explicit start time compute their first fire time
    # as "now" — meaning every process (re)start, including a --reload cycle
    # during development, fires this immediately. Pinning next_run_time to
    # one interval out avoids that.
    _scheduler.add_job(_run_refund_intake, "interval", minutes=settings.rf_sync_interval_minutes,
                       id="refund_intake", replace_existing=True,
                       next_run_time=now + timedelta(minutes=settings.rf_sync_interval_minutes))

    _scheduler.add_job(_run_course_master, CronTrigger(hour=3, minute=0, timezone=settings.tz),
                       id="course_master", replace_existing=True)

    _scheduler.add_job(_run_reassignment_sweep, CronTrigger(hour=4, minute=0, timezone=settings.tz),
                       id="reassignment_sweep", replace_existing=True)

    _scheduler.start()
    log.info("Scheduler started: full sync at %s, refund intake every %sm, course master daily at "
            "03:00, reassignment sweep daily at 04:00.",
            settings.schedule_times, settings.rf_sync_interval_minutes)
    return _scheduler


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
