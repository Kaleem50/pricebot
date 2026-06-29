"""
api/routers/platforms.py — Platform Connection Management Endpoints

Handles connecting, validating, and managing platform integrations.

Security invariants (SECURITY.md §3):
  - Platform credentials are ALWAYS encrypted before DB write.
  - encrypted_creds is NEVER returned in any API response.
  - user_id is ALWAYS sourced from the validated JWT (never from request body).
  - Every DB query filters by current_user.id.

Endpoints:
  GET  /platforms                      List connected platforms + status
  POST /platforms/{platform}/connect   Validate credentials, encrypt, store
  DELETE /platforms/{platform}         Deactivate connection + fail active jobs
  POST /platforms/{platform}/sync      Trigger product catalog sync from platform
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from api.dependencies import AuthenticatedUser, get_current_user, get_db
from core.crypto import decrypt_credential, encrypt_credential
from platforms import ALL_PLATFORMS, get_connector
from platforms.exceptions import PlatformAuthError, PlatformError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platforms", tags=["platforms"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PlatformConnectionResponse(BaseModel):
    """Public representation of a platform_connection row — never includes encrypted_creds."""

    id: str
    platform: str
    is_active: bool
    shop_identifier: str | None
    last_validated: datetime | None
    created_at: datetime


class ConnectPlatformRequest(BaseModel):
    """
    Request body for POST /platforms/{platform}/connect.

    credentials keys are platform-specific (e.g. refresh_token, client_id for Amazon).
    """

    credentials: dict[str, str]
    shop_identifier: str | None = None


class ConnectPlatformResponse(BaseModel):
    """Response after a successful platform connection."""

    platform: str
    shop_identifier: str | None
    is_active: bool
    message: str


class SyncResponse(BaseModel):
    """Response after a product catalog sync."""

    platform: str
    products_synced: int
    message: str


# ---------------------------------------------------------------------------
# GET /platforms
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PlatformConnectionResponse])
async def list_platforms(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> list[dict]:
    """
    Return all platform connections for the authenticated user.

    encrypted_creds is excluded from the DB query — it is never sent over the wire.
    """
    try:
        result = (
            db.table("platform_connections")
            .select("id, platform, is_active, shop_identifier, last_validated, created_at")
            .eq("user_id", current_user.id)
            .order("created_at", desc=False)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "Failed to list platform connections",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to retrieve platform connections")

    return result.data


# ---------------------------------------------------------------------------
# POST /platforms/{platform}/connect
# ---------------------------------------------------------------------------


@router.post("/{platform}/connect", response_model=ConnectPlatformResponse)
async def connect_platform(
    platform: str,
    body: ConnectPlatformRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> ConnectPlatformResponse:
    """
    Connect (or reconnect) a platform by validating credentials and storing them encrypted.

    Steps:
      1. Validate platform identifier.
      2. Instantiate the connector and call validate_credentials().
      3. Encrypt the credentials with AES-256-GCM.
      4. Upsert the platform_connection row (one row per user+platform).

    Returns 400 if validation fails, 501 if connector is not yet built.
    """
    if platform not in ALL_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown platform: {platform!r}. Must be one of: {ALL_PLATFORMS}",
        )

    try:
        connector = get_connector(
            platform=platform,
            credentials=body.credentials,
            user_id=current_user.id,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        is_valid = await connector.validate_credentials()
    except PlatformAuthError as exc:
        logger.warning(
            "Platform credential validation rejected",
            extra={"user_id": current_user.id, "platform": platform},
        )
        raise HTTPException(status_code=400, detail=f"Credential validation failed: {exc}")
    except PlatformError as exc:
        logger.error(
            "Platform API error during credential validation",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=f"Platform API error: {exc}")

    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail="Credentials are invalid. Check your API keys and try again.",
        )

    # Encrypt credentials — plaintext never leaves this scope
    creds_json = json.dumps(body.credentials)
    try:
        encrypted = encrypt_credential(creds_json)
    except RuntimeError as exc:
        logger.critical(
            "Credential encryption failed",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Credential encryption service error")

    row = {
        "user_id": current_user.id,
        "platform": platform,
        "encrypted_creds": encrypted,
        "shop_identifier": body.shop_identifier,
        "is_active": True,
        "last_validated": datetime.now(timezone.utc).isoformat(),
    }

    try:
        db.table("platform_connections").upsert(
            row, on_conflict="user_id,platform"
        ).execute()
    except Exception as exc:
        logger.error(
            "Failed to upsert platform connection",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to save platform connection")

    logger.info(
        "Platform connected",
        extra={
            "user_id": current_user.id,
            "platform": platform,
            "shop_identifier": body.shop_identifier,
        },
    )

    return ConnectPlatformResponse(
        platform=platform,
        shop_identifier=body.shop_identifier,
        is_active=True,
        message=f"{platform.title()} connected successfully.",
    )


# ---------------------------------------------------------------------------
# DELETE /platforms/{platform}
# ---------------------------------------------------------------------------


@router.delete("/{platform}", status_code=204)
async def disconnect_platform(
    platform: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> None:
    """
    Deactivate a platform connection and fail any active repricing jobs.

    Sets is_active=False on the connection.
    Sets state=FAILED on any IDLE or BATCH_SUBMITTED repricing jobs for this platform.
    """
    try:
        result = (
            db.table("platform_connections")
            .select("id")
            .eq("user_id", current_user.id)
            .eq("platform", platform)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error looking up platform connection",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"No {platform} connection found for this account.",
        )

    connection_id = result.data[0]["id"]

    try:
        db.table("platform_connections").update(
            {
                "is_active": False,
                "invalidated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", connection_id).eq("user_id", current_user.id).execute()
    except Exception as exc:
        logger.error(
            "Failed to deactivate platform connection",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to deactivate connection")

    # Fail active repricing jobs so the scheduler does not pick them up
    for active_state in ("IDLE", "BATCH_SUBMITTED"):
        try:
            db.table("repricing_jobs").update(
                {
                    "state": "FAILED",
                    "fail_reason": f"Platform {platform} disconnected by user",
                }
            ).eq("user_id", current_user.id).eq("platform", platform).eq(
                "state", active_state
            ).execute()
        except Exception as exc:
            logger.warning(
                "Could not fail some repricing jobs on platform disconnect",
                extra={
                    "user_id": current_user.id,
                    "platform": platform,
                    "state": active_state,
                    "error": str(exc),
                },
            )

    logger.info(
        "Platform disconnected",
        extra={"user_id": current_user.id, "platform": platform},
    )


# ---------------------------------------------------------------------------
# POST /platforms/{platform}/sync
# ---------------------------------------------------------------------------


@router.post("/{platform}/sync", response_model=SyncResponse)
async def sync_platform(
    platform: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> SyncResponse:
    """
    Trigger a product catalog sync from the platform to PriceBot.

    Fetches the seller's listings and upserts each product to the products table.
    Cost and min_margin_floor set by the seller in the dashboard are preserved —
    the upsert only updates price, title, and sync timestamp.
    """
    try:
        conn_result = (
            db.table("platform_connections")
            .select("encrypted_creds, is_active")
            .eq("user_id", current_user.id)
            .eq("platform", platform)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error fetching platform connection for sync",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not conn_result.data:
        raise HTTPException(
            status_code=404,
            detail=f"No {platform} connection found. Connect {platform} first.",
        )

    conn = conn_result.data[0]
    if not conn["is_active"]:
        raise HTTPException(
            status_code=400,
            detail=f"{platform} connection is inactive. Reconnect to resume syncing.",
        )

    try:
        creds_dict: dict[str, str] = json.loads(decrypt_credential(conn["encrypted_creds"]))
    except Exception as exc:
        logger.critical(
            "Failed to decrypt platform credentials",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Credential decryption error")

    try:
        connector = get_connector(
            platform=platform, credentials=creds_dict, user_id=current_user.id
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))

    try:
        products = await connector.get_products()
    except PlatformAuthError:
        # Deactivate so user is prompted to reconnect
        db.table("platform_connections").update({"is_active": False}).eq(
            "user_id", current_user.id
        ).eq("platform", platform).execute()
        raise HTTPException(
            status_code=401,
            detail="Platform credentials have expired. Reconnect your account.",
        )
    except PlatformError as exc:
        logger.error(
            "Platform error during sync",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=f"Platform sync error: {exc}")

    synced = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for product in products:
        row = {
            "user_id": current_user.id,
            "platform": platform,
            "platform_product_id": product.platform_product_id,
            "platform_sku": product.platform_sku,
            "title": product.title,
            "current_price": product.current_price,
            "last_synced_at": now_iso,
        }
        try:
            db.table("products").upsert(
                row, on_conflict="user_id,platform,platform_product_id"
            ).execute()
            synced += 1
        except Exception as exc:
            logger.warning(
                "Failed to upsert product during sync",
                extra={
                    "user_id": current_user.id,
                    "platform_product_id": product.platform_product_id,
                    "error": str(exc),
                },
            )

    logger.info(
        "Platform sync complete",
        extra={
            "user_id": current_user.id,
            "platform": platform,
            "products_synced": synced,
        },
    )

    return SyncResponse(
        platform=platform,
        products_synced=synced,
        message=f"Synced {synced} products from {platform.title()}.",
    )
