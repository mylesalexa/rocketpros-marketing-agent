"""
RocketPros Marketing Agent — Main Entry Point
Runs on Railway PRO. Schedules the daily pipeline via APScheduler.

Schedule: Daily at 8:00 AM CT (14:00 UTC)
Override with CRON_HOUR and CRON_MINUTE env vars.
"""

import os
import sys
import logging
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("rocketpros-agent")

# Schedule config — 8 AM CT = 14:00 UTC (no DST adjustment; adjust if needed)
CRON_HOUR = int(os.getenv("CRON_HOUR", "14"))    # UTC hour
CRON_MINUTE = int(os.getenv("CRON_MINUTE", "0"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


def run_daily_pipeline():
    """Wrapper that imports and runs the pipeline (isolated per-run)."""
    log.info("Daily pipeline triggered")
    try:
        from pipeline import run_pipeline
        summary = run_daily_pipeline_and_save(run_pipeline)
        error_count = len(summary.get("errors", []))
        log.info(
            f"Pipeline complete — "
            f"{summary.get('articles_succeeded', 0)} articles, "
            f"{error_count} error(s)"
        )
    except Exception as e:
        log.exception(f"Pipeline crashed: {e}")


def run_daily_pipeline_and_save(run_pipeline_fn):
    import json
    from pathlib import Path

    summary = run_pipeline_fn(dry_run=DRY_RUN)
    summary_path = Path("output/last_run.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return summary


def main():
    log.info("=" * 50)
    log.info("RocketPros Marketing Agent starting up")
    log.info(f"Scheduled: daily at {CRON_HOUR:02d}:{CRON_MINUTE:02d} UTC")
    log.info(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    log.info("=" * 50)

    # Check if --run-now flag is set (useful for Railway one-off runs)
    if "--run-now" in sys.argv:
        log.info("--run-now flag detected, executing pipeline immediately")
        run_daily_pipeline()
        return

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_daily_pipeline,
        trigger=CronTrigger(hour=CRON_HOUR, minute=CRON_MINUTE),
        id="daily_pipeline",
        name="RocketPros Daily Marketing Pipeline",
        replace_existing=True,
        misfire_grace_time=3600,  # Allow up to 1 hour late start
    )

    log.info(f"Scheduler started. Next run: {scheduler.get_jobs()[0].next_run_time}")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
