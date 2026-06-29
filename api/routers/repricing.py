"""
api/routers/repricing.py — Repricing Job Endpoints (stub)

Planned endpoints (Week 2):
  - GET  /repricing/history          — Paginated price change log
  - GET  /repricing/jobs             — Active + recent job states
  - POST /repricing/jobs/{id}/retry  — Reset a FAILED job back to IDLE
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/repricing", tags=["repricing"])
