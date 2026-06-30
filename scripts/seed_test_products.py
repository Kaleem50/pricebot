#!/usr/bin/env python3
"""
scripts/seed_test_products.py — Populate Test Data for Worker Pipeline Testing

Creates test products and platform connection in Supabase for end-to-end
testing of the repricing worker pipeline without real platform credentials.

Usage:
    python3 scripts/seed_test_products.py [--user-id USER_ID]

If --user-id is not provided, prompts for one interactively.

Creates:
  - 1 platform_connections row (platform='amazon', encrypted mock credentials)
  - 4 products (prod-a, prod-b, prod-c, prod-d) with state=IDLE
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load environment before importing PriceBot modules
load_dotenv()

from db.client import get_db
from core.crypto import encrypt_credential

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Test products fixture (matches platforms/mock.py)
TEST_PRODUCTS = [
    {
        "id": "prod-a",
        "platform": "amazon",
        "platform_product_id": "ASIN-A001",
        "platform_sku": "SKU-A001",
        "title": "Test Product A - Normal Case",
        "current_price": 24.99,
        "cost": 12.00,
        "min_margin_floor": 3.60,
    },
    {
        "id": "prod-b",
        "platform": "amazon",
        "platform_product_id": "ASIN-B001",
        "platform_sku": "SKU-B001",
        "title": "Test Product B - Guardrail Trigger",
        "current_price": 19.99,
        "cost": 15.00,
        "min_margin_floor": 8.00,
    },
    {
        "id": "prod-c",
        "platform": "amazon",
        "platform_product_id": "ASIN-C001",
        "platform_sku": "SKU-C001",
        "title": "Test Product C - Premium Case",
        "current_price": 49.99,
        "cost": 20.00,
        "min_margin_floor": 5.00,
    },
    {
        "id": "prod-d",
        "platform": "amazon",
        "platform_product_id": "ASIN-D001",
        "platform_sku": "SKU-D001",
        "title": "Test Product D - Error Handling",
        "current_price": 15.00,
        "cost": 8.00,
        "min_margin_floor": 2.00,
    },
]


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


def seed_platform_connection(db, user_id: str) -> None:
    """Create a mock platform_connections row for testing."""
    # Encrypt dummy credentials for mock connector
    creds = {
        "refresh_token": "mock-token",
        "client_id": "mock-client",
        "client_secret": "mock-secret",
        "marketplace_id": "ATVPDKIKX0DER",
        "merchant_id": "mock-merchant",
    }
    creds_json = json.dumps(creds)

    try:
        key_hex = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "a" * 64)
        encrypted_creds = encrypt_credential(creds_json, key_hex)
    except Exception as exc:
        logger.error(f"Failed to encrypt credentials: {exc}")
        sys.exit(1)

    try:
        db.table("platform_connections").upsert(
            {
                "user_id": user_id,
                "platform": "amazon",
                "encrypted_creds": encrypted_creds,
                "is_active": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("user_id", user_id).eq("platform", "amazon").execute()

        logger.info(f"✓ Platform connection created for user {user_id}")
    except Exception as exc:
        logger.error(f"Failed to create platform_connections row: {exc}")
        sys.exit(1)


def seed_products(db, user_id: str) -> list[str]:
    """Create test products in IDLE state. Return list of product IDs."""
    product_ids = []

    for prod_data in TEST_PRODUCTS:
        try:
            result = db.table("products").upsert(
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
                }
            ).eq("id", prod_data["id"]).execute()

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
    args = parser.parse_args()

    user_id = get_user_id(args.user_id)

    # Validate environment
    if not os.environ.get("SUPABASE_URL"):
        logger.error("SUPABASE_URL environment variable not set")
        sys.exit(1)

    if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        logger.error("SUPABASE_SERVICE_ROLE_KEY environment variable not set")
        sys.exit(1)

    logger.info("\n📦 Seeding test data for worker pipeline testing...\n")

    try:
        db = get_db()
    except Exception as exc:
        logger.error(f"Failed to connect to Supabase: {exc}")
        sys.exit(1)

    # Create platform connection
    seed_platform_connection(db, user_id)

    # Create products
    product_ids = seed_products(db, user_id)

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
