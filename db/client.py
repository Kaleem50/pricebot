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

CRITICAL — get_db() vs get_auth_client() (post-mortem, 2026-07-01):
  ``get_db()``'s singleton must NEVER have ``auth.sign_up()``,
  ``auth.sign_in_with_password()``, or ``auth.refresh_session()`` called on
  it, anywhere in the codebase. Those three GoTrue calls persist the
  resulting end-user session onto the shared client's underlying PostgREST
  ``Authorization`` header — silently downgrading every subsequent
  ``.table(...)`` query on the singleton from ``service_role`` to whichever
  user most recently authenticated, for every other concurrent or later
  request in the process, with no error or log signal. This was a live
  Critical-severity tenant-isolation bug (see QA report, 2026-07-01):
  registering or logging in as any user silently corrupted every other
  user's product/platform/billing queries until process restart.
  ``auth.get_user(token)`` (token verification only) does NOT mutate session
  state and is safe to call on ``get_db()`` — only the three session-
  establishing calls above are dangerous. Use ``get_auth_client()`` for
  those three operations instead.
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

    Never call ``auth.sign_up()``, ``auth.sign_in_with_password()``, or
    ``auth.refresh_session()`` on the client this returns — see the module
    docstring. Use ``get_auth_client()`` for those operations.

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


def get_auth_client() -> Client:
    """
    Return a fresh, request-scoped Supabase client for end-user auth
    operations: ``sign_up()``, ``sign_in_with_password()``, ``refresh_session()``.

    Deliberately NOT cached — a new client is constructed on every call so
    the session each of these calls establishes is scoped to a single
    request and discarded immediately afterward. It must never be reused
    across requests or shared with ``get_db()``'s singleton (see module
    docstring for why: those calls mutate session state in a way that would
    otherwise leak across every concurrent request sharing the client).

    Uses ``SUPABASE_ANON_KEY`` — the correct, RLS-respecting key for
    end-user authentication flows (as opposed to ``get_db()``'s
    service-role key, which must stay reserved for already-authenticated,
    user_id-filtered application queries).

    Returns:
        A new Supabase ``Client`` instance, not cached.

    Raises:
        KeyError: If ``SUPABASE_URL`` or ``SUPABASE_ANON_KEY`` are not set
                  in the environment.
    """
    url = os.environ["SUPABASE_URL"]
    anon_key = os.environ["SUPABASE_ANON_KEY"]
    return create_client(url, anon_key)
