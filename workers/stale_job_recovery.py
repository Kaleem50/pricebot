"""
workers/stale_job_recovery.py — Stale Job Recovery

Detects and recovers jobs stuck in intermediate states due to worker crashes,
timeouts, or infrastructure failures.  Runs every hour.

Detection rules:
  - BATCH_SUBMITTED > 2 hours → mark FAILED with reason='batch_timeout'
  - PROCESSING > 30 min → mark FAILED with reason='applicator_timeout'
  - FAILED > 1 hour AND retry_count < 3 → reset to IDLE, increment retry_count

This ensures the repricing pipeline never hangs indefinitely.  Jobs in terminal
states (SYNCED) are left alone.  IDLE jobs are processed on the next submission
cycle.

Security constraints (CLAUDE.md §5.4):
  - Every DB query filters by user_id.
  - No credentials are accessed or logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from supabase import Client

logger = logging.getLogger(__name__)


class StaleJobRecovery:
    """
    Detects and recovers stuck repricing jobs.

    Typical usage (called by scheduler every 60 min)::

        recovery = StaleJobRecovery()
        result = recovery.recover(db=db_client)

        logger.info("Recovery cycle complete", extra=result)
    """

    def recover(self, db: Client) -> dict[str, Any]:
        """
        Scan repricing_jobs for stuck states and recover them.

        Flow:
          1. Query BATCH_SUBMITTED jobs updated > 2 hours ago → mark FAILED.
          2. Query PROCESSING jobs updated > 30 min ago → mark FAILED.
          3. Query FAILED jobs updated > 1 hour ago AND retry_count < 3 → reset to IDLE.
          4. Log each recovery as WARNING.

        Args:
            db: Supabase client.

        Returns:
            Dict with keys:
              - recovered: int (jobs reset to IDLE)
              - timed_out: int (jobs marked FAILED due to timeout)
        """
        logger.info("Stale job recovery: starting scan")

        recovered = 0
        timed_out = 0

        now = datetime.now(timezone.utc)

        # Step 1: BATCH_SUBMITTED > 2 hours
        logger.info("Scanning BATCH_SUBMITTED jobs for timeout")
        batch_submitted_cutoff = now - timedelta(hours=2)

        try:
            stale_submitted = (
                db.table("repricing_jobs")
                .select("id, user_id, product_id, platform")
                .eq("state", "BATCH_SUBMITTED")
                .lt("updated_at", batch_submitted_cutoff.isoformat())
                .execute()
            )
        except Exception as exc:
            logger.error(
                "Failed to query stale BATCH_SUBMITTED jobs",
                extra={"error": str(exc)},
            )
            raise

        for job in stale_submitted.data or []:
            job_id = job["id"]
            user_id = job["user_id"]
            product_id = job["product_id"]

            logger.warning(
                "Stale job detected: BATCH_SUBMITTED timeout",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "product_id": product_id,
                    "state": "BATCH_SUBMITTED",
                    "timeout_hours": 2,
                },
            )

            try:
                db.table("repricing_jobs").update(
                    {
                        "state": "FAILED",
                        "fail_reason": "batch_timeout (>2 hours BATCH_SUBMITTED)",
                        "completed_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    }
                ).eq("id", job_id).execute()
                timed_out += 1
            except Exception as exc:
                logger.error(
                    "Failed to update stale BATCH_SUBMITTED job to FAILED",
                    extra={"job_id": job_id, "error": str(exc)},
                )

        # Step 2: PROCESSING > 30 minutes
        logger.info("Scanning PROCESSING jobs for timeout")
        processing_cutoff = now - timedelta(minutes=30)

        try:
            stale_processing = (
                db.table("repricing_jobs")
                .select("id, user_id, product_id, platform")
                .eq("state", "PROCESSING")
                .lt("updated_at", processing_cutoff.isoformat())
                .execute()
            )
        except Exception as exc:
            logger.error(
                "Failed to query stale PROCESSING jobs",
                extra={"error": str(exc)},
            )
            raise

        for job in stale_processing.data or []:
            job_id = job["id"]
            user_id = job["user_id"]
            product_id = job["product_id"]

            logger.warning(
                "Stale job detected: PROCESSING timeout",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "product_id": product_id,
                    "state": "PROCESSING",
                    "timeout_minutes": 30,
                },
            )

            try:
                db.table("repricing_jobs").update(
                    {
                        "state": "FAILED",
                        "fail_reason": "applicator_timeout (>30 min PROCESSING)",
                        "completed_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    }
                ).eq("id", job_id).execute()
                timed_out += 1
            except Exception as exc:
                logger.error(
                    "Failed to update stale PROCESSING job to FAILED",
                    extra={"job_id": job_id, "error": str(exc)},
                )

        # Step 3: FAILED > 1 hour, retry_count < 3 → reset to IDLE
        logger.info("Scanning FAILED jobs for auto-retry")
        failed_cutoff = now - timedelta(hours=1)

        try:
            recoverable_failed = (
                db.table("repricing_jobs")
                .select("id, user_id, product_id, platform, retry_count")
                .eq("state", "FAILED")
                .lt("updated_at", failed_cutoff.isoformat())
                .lt("retry_count", 3)
                .execute()
            )
        except Exception as exc:
            logger.error(
                "Failed to query recoverable FAILED jobs",
                extra={"error": str(exc)},
            )
            raise

        for job in recoverable_failed.data or []:
            job_id = job["id"]
            user_id = job["user_id"]
            product_id = job["product_id"]
            retry_count = job.get("retry_count") or 0

            logger.warning(
                "Stale job detected: FAILED eligible for retry",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "product_id": product_id,
                    "state": "FAILED",
                    "retry_count": retry_count,
                    "timeout_hours": 1,
                },
            )

            try:
                db.table("repricing_jobs").update(
                    {
                        "state": "IDLE",
                        "retry_count": retry_count + 1,
                        "fail_reason": None,
                        "batch_id": None,
                        "updated_at": now.isoformat(),
                    }
                ).eq("id", job_id).execute()
                recovered += 1
            except Exception as exc:
                logger.error(
                    "Failed to reset FAILED job to IDLE",
                    extra={"job_id": job_id, "error": str(exc)},
                )

        logger.info(
            "Stale job recovery cycle complete",
            extra={
                "recovered": recovered,
                "timed_out": timed_out,
            },
        )

        return {
            "recovered": recovered,
            "timed_out": timed_out,
        }
