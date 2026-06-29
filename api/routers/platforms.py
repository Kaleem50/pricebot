"""
api/routers/platforms.py — Platform Connection Endpoints (stub)

Planned endpoints (Week 2):
  - GET  /platforms                      — List connected platforms + status
  - POST /platforms/{platform}/connect   — Store encrypted credentials
  - DELETE /platforms/{platform}         — Remove credentials + cancel active jobs
  - POST /platforms/{platform}/sync      — Trigger manual product catalog sync
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/platforms", tags=["platforms"])
