"""
api/routers/billing.py — Billing and Subscription Endpoints (stub)

Planned endpoints (Week 2):
  - GET  /billing/subscription  — Current plan, usage stats, next billing date
  - POST /billing/portal        — Create Stripe customer portal session URL
  - POST /billing/webhook       — Stripe webhook (public, HMAC-verified)
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/billing", tags=["billing"])
