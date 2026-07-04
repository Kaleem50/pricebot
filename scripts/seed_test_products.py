#!/usr/bin/env python3
"""
scripts/seed_test_products.py — Populate Test Data for Worker Pipeline Testing

Creates test products and platform connection in Supabase for end-to-end
testing of the repricing worker pipeline without real platform credentials.

Usage:
    python3 scripts/seed_test_products.py [--user-id USER_ID] [--platform PLATFORM]

If --user-id is not provided, prompts for one interactively.
--platform defaults to 'amazon'. Use '--platform etsy' for Etsy test products.

Creates:
  - 1 platform_connections row (encrypted mock credentials)
  - 4 products with state=IDLE (Amazon) or 2 products (Etsy)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

# Add parent directory to path so we can import PriceBot modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load environment before importing PriceBot modules
load_dotenv()

from db.client import get_db
from core.crypto import encrypt_credential

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Test products fixture (deterministic UUIDs for reproducibility)
AMAZON_TEST_PRODUCTS = [
    {
        "id": "098abf69-9ad0-5931-a09b-8f2d8d1d5289",  # prod-a
        "platform": "amazon",
        "platform_product_id": "ASIN-A001",
        "platform_sku": "SKU-A001",
        "title": "Test Product A - Normal Case",
        "current_price": 24.99,
        "cost": 12.00,
        "min_margin_floor": 3.60,
    },
    {
        "id": "f882dfc7-f431-5d5d-857f-ec8f71b71669",  # prod-b
        "platform": "amazon",
        "platform_product_id": "ASIN-B001",
        "platform_sku": "SKU-B001",
        "title": "Test Product B - Guardrail Trigger",
        "current_price": 19.99,
        "cost": 15.00,
        "min_margin_floor": 8.00,
    },
    {
        "id": "b69bf742-1304-54e7-9978-260b2dae62bb",  # prod-c
        "platform": "amazon",
        "platform_product_id": "ASIN-C001",
        "platform_sku": "SKU-C001",
        "title": "Test Product C - Premium Case",
        "current_price": 49.99,
        "cost": 20.00,
        "min_margin_floor": 5.00,
    },
    {
        "id": "8894b55e-4450-56dc-bf82-a890602952c0",  # prod-d
        "platform": "amazon",
        "platform_product_id": "ASIN-D001",
        "platform_sku": "SKU-D001",
        "title": "Test Product D - Error Handling",
        "current_price": 15.00,
        "cost": 8.00,
        "min_margin_floor": 2.00,
    },
]

ETSY_TEST_PRODUCTS = [
    {
        "id": "c1a2b3d4-e5f6-7890-abcd-ef1234567891",  # etsy-prod-a
        "platform": "etsy",
        "platform_product_id": "100000001",
        "platform_sku": None,
        "title": "Handmade Ceramic Coffee Mug",
        "current_price": 28.00,
        "cost": 8.00,
        "min_margin_floor": 4.00,
    },
    {
        "id": "d2b3c4e5-f6a7-8901-bcde-f12345678902",  # etsy-prod-b
        "platform": "etsy",
        "platform_product_id": "100000002",
        "platform_sku": None,
        "title": "Custom Engraved Wooden Cutting Board",
        "current_price": 45.00,
        "cost": 15.00,
        "min_margin_floor": 6.00,
    },
]

# Keep backward-compatible alias
TEST_PRODUCTS = AMAZON_TEST_PRODUCTS


def get_user_id(provided: str | None = None) -> str:
    """Get user_id from argument or prompt interactively."""
    if provided:
        return provided

    print("\n🔑 Enter your Supabase user UUID for test data:")
    print("   (Find it in Supabase > Authentication > Users)")
    user_id = input("User ID: ").strip()

    if not user_id:
        logger.error("User ID cannot be empty")
        sys.exit(1)

    return user_id


def seed_platform_connection(db, user_id: str, platform: str) -> None:
    """Create a mock platform_connections row for testing."""
    if platform == "etsy":
        creds = {
            "access_token": "mock-etsy-access-token",
            "refresh_token": "mock-etsy-refresh-token",
            "shop_id": "99999999",
        }
    else:
        creds = {
            "refresh_token": "mock-token",
            "client_id": "mock-client",
            "client_secret": "mock-secret",
            "marketplace_id": "ATVPDKIKX0DER",
            "merchant_id": "mock-merchant",
        }

    creds_json = json.dumps(creds)

    try:
        encrypted_creds = encrypt_credential(creds_json)
    except Exception as exc:
        logger.error(f"Failed to encrypt credentials: {exc}")
        sys.exit(1)

    try:
        db.table("platform_connections").upsert(
            {
                "user_id": user_id,
                "platform": platform,
                "encrypted_creds": encrypted_creds,
                "is_active": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id,platform"
        ).execute()

        logger.info(f"✓ Platform connection created for user {user_id} ({platform})")
    except Exception as exc:
        logger.error(f"Failed to create platform_connections row: {exc}")
        sys.exit(1)


def seed_products(db, user_id: str, platform: str = "amazon") -> list[str]:
    """Create test products in IDLE state. Return list of product IDs."""
    product_ids = []
    products = ETSY_TEST_PRODUCTS if platform == "etsy" else AMAZON_TEST_PRODUCTS

    for prod_data in products:
        try:
            db.table("products").upsert(
                {
                    "id": prod_data["id"],
                    "user_id": user_id,
                    "platform": prod_data["platform"],
                    "platform_product_id": prod_data["platform_product_id"],
                    "platform_sku": prod_data["platform_sku"],
                    "title": prod_data["title"],
                    "current_price": prod_data["current_price"],
                    "cost": prod_data["cost"],
                    "min_margin_floor": prod_data["min_margin_floor"],
                    "is_tracking": True,
                    "state": "IDLE",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="id"
            ).execute()

            product_ids.append(prod_data["id"])
            logger.info(f"✓ Product {prod_data['id']}: {prod_data['title']}")
        except Exception as exc:
            logger.error(f"Failed to create product {prod_data['id']}: {exc}")
            sys.exit(1)

    return product_ids


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Seed test products for worker pipeline testing"
    )
    parser.add_argument(
        "--user-id",
        type=str,
        default=None,
        help="Supabase user UUID (prompted if not provided)",
    )
    parser.add_argument(
        "--platform",
        type=str,
        default="amazon",
        choices=["amazon", "etsy"],
        help="Platform to seed test products for (default: amazon)",
    )
    args = parser.parse_args()

    user_id = get_user_id(args.user_id)
    platform = args.platform

    # Validate environment
    if not os.environ.get("SUPABASE_URL"):
        logger.error("SUPABASE_URL environment variable not set")
        sys.exit(1)

    if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        logger.error("SUPABASE_SERVICE_ROLE_KEY environment variable not set")
        sys.exit(1)

    logger.info(f"\n📦 Seeding test data for {platform} worker pipeline testing...\n")

    try:
        db = get_db()
    except Exception as exc:
        logger.error(f"Failed to connect to Supabase: {exc}")
        sys.exit(1)

    # Create platform connection
    seed_platform_connection(db, user_id, platform)

    # Create products
    product_ids = seed_products(db, user_id, platform)

    # Print summary
    logger.info("\n✅ Test data seeded successfully!\n")
    logger.info(f"User ID: {user_id}")
    logger.info(f"Product IDs: {', '.join(product_ids)}\n")
    logger.info("Next steps:")
    logger.info("  1. Set MOCK_PLATFORM_MODE=true in your environment")
    logger.info("  2. Run the worker pipeline:")
    logger.info("     POST /repricing/trigger-cycle")
    logger.info("  3. Watch worker logs for pricing recommendations\n")


if __name__ == "__main__":
    main()
