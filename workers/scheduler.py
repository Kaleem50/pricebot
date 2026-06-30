"""
workers/scheduler.py — APScheduler Orchestration

Background scheduler that runs three cyclic jobs:
  - run_submission_cycle() every 15 min: collects IDLE products and submits to Anthropic.
  - run_poll_cycle() every 5 min: polls batches for completion and applies prices.
  - run_recovery_cycle() every 60 min: detects and recovers stuck jobs.

Deployment model:
  - Runs as a separate process (not in FastAPI).  Reads ANTHROPIC_API_KEY and
    Supabase credentials from environment at startup.
  - Uses dotenv to load .env file (separate process context).
  - APScheduler runs jobs in parallel but submission cycle is protected by a
    thread lock to prevent concurrent submissions.
  - Graceful shutdown on SIGTERM (drains pending jobs before exit).

Logging:
  - Logs cycle start/end with structured JSON.
  - Per-job errors are logged but do not block other jobs.
  - Critical failures (e.g., DB connection loss) are logged as CRITICAL.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Load .env into process environment BEFORE any imports of settings
load_dotenv()

from api.dependencies import Tier
from db.client import get_db
from workers.batch_poller import BatchPoller
from workers.batch_submitter import BatchSubmitter
from workers.stale_job_recovery import StaleJobRecovery

logger = logging.getLogger(__name__)

# Thread lock to ensure only one submission cycle runs at a time
_submission_lock = threading.Lock()


def run_submission_cycle() -> None:
    """
    Run the repricing submission cycle.

    Queries all active users, checks their subscription tiers, and submits
    IDLE products for repricing via the Anthropic Batch API.

    Runs every 15 minutes.
    """
    if not _submission_lock.acquire(blocking=False):
        logger.warning("Submission cycle already running — skipping this interval")
        return

    try:
        logger.info(
            "Submission cycle started",
            extra={"timestamp": datetime.now(timezone.utc).isoformat()},
        )

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            logger.critical("ANTHROPIC_API_KEY not set")
            return

        db = get_db()
        submitter = BatchSubmitter(anthropic_api_key=anthropic_key)

        # Query all active users
        try:
            subscriptions_result = (
                db.table("subscriptions")
                .select("user_id, tier")
                .in_("status", ["active", "trialing"])
                .execute()
            )
        except Exception as exc:
            logger.critical(
                "Failed to fetch active subscriptions",
                extra={"error": str(exc)},
            )
            return

        for subscription in subscriptions_result.data or []:
            user_id = subscription["user_id"]
            tier_str = subscription["tier"]

            try:
                tier = Tier.from_db(tier_str)
            except ValueError:
                logger.warning(
                    "Unknown tier value — defaulting to STARTER",
                    extra={"user_id": user_id, "tier_value": tier_str},
                )
                tier = Tier.STARTER

            try:
                result = submitter.submit_for_user(user_id=user_id, db=db, tier=tier)
                if result:
                    logger.info(
                        "User batch submitted",
                        extra={
                            "user_id": user_id,
                            **result,
                        },
                    )
                else:
                    logger.info(
                        "User submission skipped",
                        extra={"user_id": user_id},
                    )
            except Exception as exc:
                logger.error(
                    "User submission failed",
                    extra={
                        "user_id": user_id,
                        "error": str(exc),
                    },
                )

        logger.info(
            "Submission cycle completed",
            extra={"timestamp": datetime.now(timezone.utc).isoformat()},
        )

    finally:
        _submission_lock.release()


def run_poll_cycle() -> None:
    """
    Run the batch polling cycle.

    Queries BATCH_SUBMITTED jobs, checks batch completion status, retrieves
    results, and applies prices (for Growth/Pro tiers) or records suggestions
    (for Starter tier).

    Runs every 5 minutes.
    """
    logger.info(
        "Poll cycle started",
        extra={"timestamp": datetime.now(timezone.utc).isoformat()},
    )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        logger.critical("ANTHROPIC_API_KEY not set")
        return

    db = get_db()
    poller = BatchPoller(anthropic_api_key=anthropic_key)

    try:
        result = poller.poll_all_pending(db=db)
        logger.info(
            "Poll cycle completed",
            extra={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **result,
            },
        )
    except Exception as exc:
        logger.error(
            "Poll cycle failed",
            extra={
                "error": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )


def run_recovery_cycle() -> None:
    """
    Run the stale job recovery cycle.

    Detects jobs stuck in intermediate states (BATCH_SUBMITTED > 2 hours,
    PROCESSING > 30 min) and marks them FAILED, or resets FAILED jobs
    (> 1 hour, retry_count < 3) back to IDLE.

    Runs every 60 minutes.
    """
    logger.info(
        "Recovery cycle started",
        extra={"timestamp": datetime.now(timezone.utc).isoformat()},
    )

    db = get_db()
    recovery = StaleJobRecovery()

    try:
        result = recovery.recover(db=db)
        logger.info(
            "Recovery cycle completed",
            extra={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **result,
            },
        )
    except Exception as exc:
        logger.error(
            "Recovery cycle failed",
            extra={
                "error": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )


class Scheduler:
    """
    APScheduler-based background task orchestrator.

    Manages three cyclic jobs: submission (15 min), polling (5 min),
    and recovery (60 min).
    """

    def __init__(self) -> None:
        """Initialise the scheduler."""
        self._scheduler = BackgroundScheduler()
        self._running = False

    def start(self) -> None:
        """
        Start the scheduler and register jobs.

        Raises:
            RuntimeError: If the scheduler is already running.
        """
        if self._running:
            raise RuntimeError("Scheduler is already running")

        logger.info("Initialising APScheduler")

        self._scheduler.add_job(
            run_submission_cycle,
            "interval",
            minutes=15,
            id="submission_cycle",
            name="Repricing submission cycle",
        )

        self._scheduler.add_job(
            run_poll_cycle,
            "interval",
            minutes=5,
            id="poll_cycle",
            name="Batch polling cycle",
        )

        self._scheduler.add_job(
            run_recovery_cycle,
            "interval",
            hours=1,
            id="recovery_cycle",
            name="Stale job recovery cycle",
        )

        self._scheduler.start()
        self._running = True

        logger.info(
            "Scheduler started",
            extra={
                "jobs": [
                    "submission_cycle (15 min)",
                    "poll_cycle (5 min)",
                    "recovery_cycle (60 min)",
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    def stop(self) -> None:
        """
        Stop the scheduler gracefully.

        Waits for pending jobs to complete before shutting down.
        """
        if not self._running:
            return

        logger.info(
            "Stopping scheduler",
            extra={"timestamp": datetime.now(timezone.utc).isoformat()},
        )

        self._scheduler.shutdown(wait=True)
        self._running = False

        logger.info(
            "Scheduler stopped",
            extra={"timestamp": datetime.now(timezone.utc).isoformat()},
        )


# Global scheduler instance
_scheduler_instance: Scheduler | None = None


def get_scheduler() -> Scheduler:
    """Get or create the global scheduler instance."""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = Scheduler()
    return _scheduler_instance


def main() -> None:
    """
    Main entry point for the scheduler process.

    Sets up logging, starts the scheduler, and installs signal handlers
    for graceful shutdown on SIGTERM.
    """
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info(
        "PriceBot scheduler process starting",
        extra={"timestamp": datetime.now(timezone.utc).isoformat()},
    )

    scheduler = get_scheduler()

    def signal_handler(signum: int, frame: object) -> None:
        """Handle SIGTERM for graceful shutdown."""
        logger.info(
            "Received SIGTERM, shutting down gracefully",
            extra={"timestamp": datetime.now(timezone.utc).isoformat()},
        )
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)

    try:
        scheduler.start()
        # Keep the process alive
        signal.pause()
    except KeyboardInterrupt:
        logger.info(
            "Received KeyboardInterrupt, shutting down",
            extra={"timestamp": datetime.now(timezone.utc).isoformat()},
        )
        scheduler.stop()
        sys.exit(0)
    except Exception as exc:
        logger.critical(
            "Scheduler crashed",
            extra={
                "error": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        scheduler.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
