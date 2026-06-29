"""
api/routers/products.py — Product Catalog Endpoints (stub)

Planned endpoints (Week 2):
  - GET  /products               — Paginated, filterable product list
  - GET  /products/{id}          — Product detail + last AI suggestion
  - PATCH /products/{id}/settings — Update min_margin_floor, tracking on/off
  - POST /products/{id}/apply    — Starter: manually apply a queued suggestion
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/products", tags=["products"])
