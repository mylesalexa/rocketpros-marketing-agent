"""
RocketPros Marketing Agent — Main Entry Point
Runs on Railway PRO.

Two things run together:
  1. APScheduler — fires the daily pipeline at 8:00 AM CT (14:00 UTC)
  2. FastAPI web server — one-click publish dashboard at your Railway URL

Override schedule with CRON_HOUR / CRON_MINUTE env vars.
"""

import os
import sys
import logging
import threading
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import uvicorn

from web import app as web_app

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("rocketpros-agent")

# Schedule config — 8 AM CT = 14:00 UTC
CRON_HOUR = int(os.getenv("CRON_HOUR", "14"))
CRON_MINUTE = int(os.getenv("CRON_MINUTE", "0"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
PORT = int(os.getenv("PORT", "8000"))


def run_daily_pipeline():
    """Wrapper that imports and runs the pipeline."""
    log.info("Daily pipeline triggered")
    try:
        import json
        from pathlib import Path
        from pipeline import run_pipeline

        summary = run_pipeline(dry_run=DRY_RUN)
        summary_path = Path("output/last_run.json")
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)

        error_count = len(summary.get("errors", []))
        log.info(
            f"Pipeline complete — "
            f"{summary.get('articles_succeeded', 0)} articles, "
            f"{error_count} error(s)"
        )
    except Exception as e:
        log.exception(f"Pipeline crashed: {e}")


def run_scheduler():
    """Start APScheduler in background thread."""
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_daily_pipeline,
        trigger=CronTrigger(hour=CRON_HOUR, minute=CRON_MINUTE),
        id="daily_pipeline",
        name="RocketPros Daily Marketing Pipeline",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    log.info(f"Scheduler started — daily at {CRON_HOUR:02d}:{CRON_MINUTE:02d} UTC")
    return scheduler


def main():
    log.info("=" * 50)
    log.info("RocketPros Marketing Agent starting up")
    log.info(f"Schedule: daily at {CRON_HOUR:02d}:{CRON_MINUTE:02d} UTC")
    log.info(f"Dashboard: http://0.0.0.0:{PORT}")
    log.info(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    log.info("=" * 50)

    # --run-now: fire the pipeline once immediately (in background), then serve dashboard
    if "--run-now" in sys.argv:
        log.info("--run-now flag: firing pipeline immediately in background")
        t = threading.Thread(target=run_daily_pipeline, daemon=True)
        t.start()

    # Start scheduler in background
    scheduler = run_scheduler()

    # Run the FastAPI web dashboard (blocking — this is the main thread)
    try:
        uvicorn.run(
            web_app,
            host="0.0.0.0",
            port=PORT,
            log_level="warning",  # Keep uvicorn quiet; our logger handles the rest
        )
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
