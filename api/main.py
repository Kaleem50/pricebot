"""
api/main.py — PriceBot FastAPI Application Entrypoint

Creates and configures the FastAPI application:
  - Lifespan handler for startup/shutdown logging.
  - CORS middleware permitting the configured frontend origin.
  - RateLimiterMiddleware for blanket per-user request limiting.
  - GET /health endpoint for load-balancer and CI smoke tests.
  - Router mounts for all API domains.

Run locally with::

    make dev
    # or directly:
    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from dotenv import load_dotenv

load_dotenv()
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.middleware.rate_limiter import RateLimiterMiddleware
from api.routers import auth, billing, platforms, products, repricing

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan handler.

    Logs startup and shutdown events with enough context to confirm the
    correct environment is running.  Add resource initialisation (e.g.
    connection pool warm-up) in the startup block as the project grows.
    """
    environment = os.environ.get("ENVIRONMENT", "development")
    logger.info(
        "PriceBot API starting",
        extra={
            "environment": environment,
            "backend_url": os.environ.get("BACKEND_URL", "http://localhost:8000"),
        },
    )
    yield
    logger.info("PriceBot API shutting down", extra={"environment": environment})


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


app = FastAPI(
    title="PriceBot API",
    description="AI-powered ecommerce repricing engine — backend API.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# Middleware (applied in reverse registration order — last registered runs first)
# ---------------------------------------------------------------------------

frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RateLimiterMiddleware)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(platforms.router)
app.include_router(products.router)
app.include_router(repricing.router)
app.include_router(billing.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """
    Health check endpoint for load balancers and deployment smoke tests.

    Returns:
        JSON with ``status`` and current UTC ``timestamp``.
    """
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
