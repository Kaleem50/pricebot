"""
api/routers/auth.py — Authentication Endpoints

Delegates all auth logic to Supabase Auth.  No custom password storage,
hashing, or session management is implemented here.

Endpoints:
  - POST /auth/register — Create a new user account via Supabase Auth.
  - POST /auth/login    — Sign in and receive JWT access + refresh tokens.
  - POST /auth/refresh  — Exchange a valid refresh token for a new access token.

All token handling is done by Supabase.  The returned ``access_token`` is a
signed JWT that must be sent as ``Authorization: Bearer <token>`` on all
protected requests.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from supabase_auth.errors import AuthApiError
from pydantic import BaseModel, EmailStr, Field

from db.client import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Payload for creating a new user account."""

    email: EmailStr = Field(..., description="User's email address.")
    password: str = Field(
        ...,
        min_length=8,
        description="Password — minimum 8 characters.",
    )


class LoginRequest(BaseModel):
    """Payload for signing in to an existing account."""

    email: EmailStr = Field(..., description="Registered email address.")
    password: str = Field(..., description="Account password.")


class RefreshRequest(BaseModel):
    """Payload for refreshing an access token."""

    refresh_token: str = Field(..., description="Valid refresh token from a prior login.")


class AuthResponse(BaseModel):
    """
    Token response returned after successful register or login.

    ``access_token`` is a short-lived JWT (1 hour) to be sent as
    ``Authorization: Bearer <token>``.  ``refresh_token`` is long-lived
    (7 days) and rotated on each use.
    """

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    user_id: str = Field(..., description="Supabase user UUID.")
    email: str


class MessageResponse(BaseModel):
    """Generic message response for operations that don't return data."""

    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(body: RegisterRequest) -> AuthResponse:
    """
    Create a new user account.

    Delegates to Supabase Auth.  On success, returns JWT tokens and user info.
    The caller should immediately redirect the user to the onboarding wizard.

    Args:
        body: Email and password for the new account.

    Returns:
        ``AuthResponse`` with access and refresh tokens.

    Raises:
        HTTPException 400: If Supabase rejects the registration
                           (e.g. email already in use, weak password).
    """
    db = get_db()
    try:
        response = db.auth.sign_up({"email": body.email, "password": body.password})
    except AuthApiError as exc:
        logger.warning(
            "Registration failed",
            extra={"email": body.email, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(
            "Unexpected error during registration",
            extra={"email": body.email, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Registration service error")

    if not response.session or not response.user:
        logger.warning("Registration returned no session", extra={"email": body.email})
        raise HTTPException(
            status_code=400,
            detail="Registration failed — check your email for a confirmation link.",
        )

    logger.info("User registered successfully", extra={"user_id": response.user.id})

    return AuthResponse(
        access_token=response.session.access_token,
        refresh_token=response.session.refresh_token,
        user_id=str(response.user.id),
        email=response.user.email or body.email,
    )


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest) -> AuthResponse:
    """
    Authenticate with email and password.

    Returns JWT access and refresh tokens on success.  The access token
    expires in 1 hour; use ``/auth/refresh`` to obtain a new one.

    Args:
        body: Email and password credentials.

    Returns:
        ``AuthResponse`` with fresh access and refresh tokens.

    Raises:
        HTTPException 401: If credentials are invalid.
    """
    db = get_db()
    try:
        response = db.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except AuthApiError as exc:
        logger.info(
            "Login failed — invalid credentials",
            extra={"email": body.email},
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")
    except Exception as exc:
        logger.error(
            "Unexpected error during login",
            extra={"email": body.email, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Authentication service error")

    if not response.session or not response.user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    logger.info("User logged in", extra={"user_id": response.user.id})

    return AuthResponse(
        access_token=response.session.access_token,
        refresh_token=response.session.refresh_token,
        user_id=str(response.user.id),
        email=response.user.email or body.email,
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh(body: RefreshRequest) -> AuthResponse:
    """
    Exchange a refresh token for a new access token.

    Refresh tokens are rotated on each use — the response contains both a
    new access token and a new refresh token.  The old refresh token is
    immediately invalidated.

    Args:
        body: A valid refresh token from a prior login or refresh call.

    Returns:
        ``AuthResponse`` with a new access token and rotated refresh token.

    Raises:
        HTTPException 401: If the refresh token is invalid or expired.
    """
    db = get_db()
    try:
        response = db.auth.refresh_session(body.refresh_token)
    except AuthApiError as exc:
        logger.info("Token refresh failed", extra={"error": str(exc)})
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    except Exception as exc:
        logger.error("Unexpected error during token refresh", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail="Authentication service error")

    if not response.session or not response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    logger.info("Token refreshed", extra={"user_id": response.user.id})

    return AuthResponse(
        access_token=response.session.access_token,
        refresh_token=response.session.refresh_token,
        user_id=str(response.user.id),
        email=response.user.email or "",
    )
