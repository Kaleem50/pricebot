"""
db/client.py — Supabase Client Singleton

The only place in the entire codebase where a Supabase client is instantiated.
All database access — from API routes, workers, and middleware — must go through
the ``get_db()`` function exported from this module.

Design constraints (CLAUDE.md §5.1):
  - No other module may call ``supabase.create_client()`` directly.
  - Uses ``functools.lru_cache`` so the same client instance is returned
    on every call within a process.
  - Reads ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY`` from environment
    at first call; raises ``EnvironmentError`` if either is missing.
  - The service role key bypasses RLS — all worker queries must still include
    explicit ``user_id`` filters (SECURITY.md §4.2).
"""

import logging
import os
from functools import lru_cache

from supabase import Client, create_client

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_db() -> Client:
    """
    Return the shared Supabase client singleton.

    Instantiated once per process on first call; subsequent calls return the
    cached instance.  Uses ``SUPABASE_SERVICE_ROLE_KEY`` which bypasses RLS —
    worker code must always filter queries by ``user_id`` explicitly.

    Returns:
        Supabase ``Client`` instance connected to the project URL.

    Raises:
        KeyError: If ``SUPABASE_URL`` or ``SUPABASE_SERVICE_ROLE_KEY`` are
                  not set in the environment.
    """
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    logger.info(
        "Initialising Supabase client",
        extra={"supabase_url": url},
    )

    client: Client = create_client(url, key)

    logger.info("Supabase client initialised successfully")
    return client
