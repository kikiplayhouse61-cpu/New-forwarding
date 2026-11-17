"""
Background scheduler.
Uses APScheduler with SQLAlchemy job store for persistence across restarts.
"""
import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

import database as db
import bot as tg_bot

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "forwarder.db")
_scheduler: BackgroundScheduler | None = None
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()

# Admin notification queue — Flask picks these up for SSE / polling
_notification_queue: list[dict] = []


def get_scheduler() -> BackgroundScheduler:
    global _scheduler, _loop
    with _lock:
        if _scheduler is None:
            jobstores = {
                "default": SQLAlchemyJobStore(
                    url=f"sqlite:///{DB_PATH}",
                    tablename="apscheduler_jobs"
                )
            }
            executors = {"default": ThreadPoolExecutor(10)}
            _scheduler = BackgroundScheduler(
                jobstores=jobstores,
                executors=executors,
                timezone="UTC"
            )
            _scheduler.start()
            logger.info("Scheduler started")

            # Dedicated asyncio loop for async tasks
            _loop = asyncio.new_event_loop()
            t = threading.Thread(target=_loop.run_forever, daemon=True)
            t.start()

            # Recover campaigns that were running before restart
            _recover_campaigns()

        return _scheduler


def _recover_campaigns():
    campaigns = db.get_campaigns(status="running")
    for c in campaigns:
        _ensure_job(c["id"])
    logger.info("Recovered %d running campaigns", len(campaigns))


def _run_async(coro):
    """Submit an async coroutine to our dedicated loop and wait for it."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=120)


def _campaign_job(campaign_id: int):
    """Called by APScheduler thread."""
    campaign = db.get_campaign(campaign_id)
    if not campaign or campaign["status"] not in ("running",):
        return

    # Time window check
    now = datetime.now(timezone.utc)
    if campaign.get("start_time") and campaign.get("end_time"):
        try:
            start_h, start_m = map(int, campaign["start_time"].split(":"))
            end_h, end_m = map(int, campaign["end_time"].split(":"))
            cur_minutes = now.hour * 60 + now.minute
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            if not (start_minutes <= cur_minutes <= end_minutes):
                return  # Outside operating window — skip this tick
        except Exception:
            pass

    async def _notify(err_info):
        _notification_queue.append(err_info)
        if len(_notification_queue) > 500:
            _notification_queue.pop(0)

    results = _run_async(tg_bot.run_campaign(campaign_id, notify_admin_fn=_notify))
    logger.info("Campaign %d: %s", campaign_id, results)

    next_run = datetime.now(timezone.utc) + timedelta(seconds=campaign["interval_seconds"])
    db.update_campaign_status(
        campaign_id,
        status="running",
        next_run=next_run.isoformat(),
        last_run=datetime.now(timezone.utc).isoformat()
    )


def _ensure_job(campaign_id: int):
    s = get_scheduler()
    job_id = f"campaign_{campaign_id}"
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        return

    # Remove existing job if any
    if s.get_job(job_id):
        s.remove_job(job_id)

    s.add_job(
        _campaign_job,
        trigger="interval",
        seconds=max(campaign["interval_seconds"], 10),
        id=job_id,
        args=[campaign_id],
        misfire_grace_time=120,
        replace_existing=True,
    )
    logger.info("Scheduled job %s every %ds", job_id, campaign["interval_seconds"])


def start_campaign(campaign_id: int):
    _ensure_job(campaign_id)
    next_run = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    db.update_campaign_status(campaign_id, "running", next_run=next_run)


def pause_campaign(campaign_id: int):
    s = get_scheduler()
    job_id = f"campaign_{campaign_id}"
    if s.get_job(job_id):
        s.pause_job(job_id)
    db.update_campaign_status(campaign_id, "paused")


def resume_campaign(campaign_id: int):
    s = get_scheduler()
    job_id = f"campaign_{campaign_id}"
    if s.get_job(job_id):
        s.resume_job(job_id)
    else:
        _ensure_job(campaign_id)
    db.update_campaign_status(campaign_id, "running")


def stop_campaign(campaign_id: int):
    s = get_scheduler()
    job_id = f"campaign_{campaign_id}"
    if s.get_job(job_id):
        s.remove_job(job_id)
    db.update_campaign_status(campaign_id, "stopped")


def run_now(campaign_id: int) -> dict:
    """Execute campaign immediately (one-shot, outside schedule)."""
    async def _notify(err_info):
        _notification_queue.append(err_info)

    return _run_async(tg_bot.run_campaign(campaign_id, notify_admin_fn=_notify))


def get_notifications(clear: bool = True) -> list[dict]:
    items = list(_notification_queue)
    if clear:
        _notification_queue.clear()
    return items


# ── Daily stats reset job ─────────────────────────────────────────────────────

def schedule_daily_reset():
    s = get_scheduler()
    s.add_job(
        _daily_reset,
        trigger="cron",
        hour=0, minute=0,
        id="daily_reset",
        replace_existing=True,
    )


def _daily_reset():
    logger.info("Daily stats reset trigger (stats preserved, counters reset)")
    # Stats are preserved in daily_stats table; nothing to delete.
    # Reset campaign daily counters is optional — we keep them cumulative.
