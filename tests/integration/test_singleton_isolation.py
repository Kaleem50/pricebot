"""
tests/integration/test_singleton_isolation.py — Regression test for the
Critical shared-singleton DB-client contamination bug found in QA (2026-07-01).

Bug: db.client.get_db() returns a process-wide @lru_cache(maxsize=1) Supabase
Client pinned to service_role. api/routers/auth.py's /register, /login, and
/refresh endpoints used to call db.auth.sign_up() / sign_in_with_password() /
refresh_session() directly on this SAME shared client. Those GoTrue calls
persist the resulting end-user session onto the client's underlying
PostgREST Authorization header — silently downgrading every subsequent
db.table(...) query on the singleton from service_role to that user's
RLS-scoped session, for every other concurrent or later request in the
process, with no error or log signal.

Fix: api/routers/auth.py now uses db.client.get_auth_client() — a fresh,
uncached client built from the anon key, constructed per request — for
those three calls. get_db()'s singleton must never have them called on it.

This is an integration test, not a unit test: the contamination is a
property of the real supabase-py/GoTrue SDK's session handling, which a
MagicMock cannot reproduce. It requires live Supabase credentials in
.env (the same project used for manual QA) and is skipped automatically
if they are not configured.
"""

from __future__ import annotations

import os
import uuid

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

_ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".env",
)
_REAL_ENV = dotenv_values(_ENV_PATH)

_HAS_LIVE_CREDS = bool(
    _REAL_ENV.get("SUPABASE_URL")
    and _REAL_ENV.get("SUPABASE_SERVICE_ROLE_KEY")
    and _REAL_ENV.get("SUPABASE_ANON_KEY")
)

# A real seeded user known to have products in the live test Supabase project
# (see SESSION_SUMMARY.md / QA report, 2026-07-01).
_USER_A_ID = "4eb93e47-979c-4cab-814e-e25bf275524b"
_USER_A_EMAIL = "ikaleem50@gmail.com"

pytestmark = pytest.mark.skipif(
    not _HAS_LIVE_CREDS,
    reason=(
        "Requires live Supabase credentials in .env — this is an integration "
        "test of real supabase-py session behavior, which cannot be mocked"
    ),
)


@pytest.fixture
def live_env(monkeypatch: pytest.MonkeyPatch):
    """
    Apply real Supabase credentials for the duration of this test only
    (auto-reverted by monkeypatch), and clear db.client.get_db's lru_cache
    before and after so this test neither inherits a stale cached client
    nor leaks a (possibly contaminated) real client into the rest of the
    test suite.
    """
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.setenv(key, _REAL_ENV[key])

    from db.client import get_db

    get_db.cache_clear()
    yield
    get_db.cache_clear()


def _admin_client():
    from supabase import create_client

    return create_client(_REAL_ENV["SUPABASE_URL"], _REAL_ENV["SUPABASE_SERVICE_ROLE_KEY"])


def _get_token_for_user(email: str) -> str:
    """Mint a fresh access token via Supabase Admin API magic-link + OTP exchange."""
    import requests

    resp = _admin_client().auth.admin.generate_link({"type": "magiclink", "email": email})
    r = requests.post(
        f"{_REAL_ENV['SUPABASE_URL']}/auth/v1/verify",
        json={"token_hash": resp.properties.hashed_token, "type": "magiclink"},
        headers={"apikey": _REAL_ENV["SUPABASE_ANON_KEY"], "Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _delete_user_by_email(email: str) -> None:
    """Best-effort cleanup of a throwaway test user created during a test run."""
    admin = _admin_client()
    for u in admin.auth.admin.list_users():
        if u.email == email:
            admin.auth.admin.delete_user(u.id)
            return


class TestSingletonIsolation:
    """Reproduces the exact QA repro steps against the real FastAPI app + real DB."""

    def test_register_then_login_does_not_break_other_users_product_queries(self, live_env):
        """
        THE regression test for the fix. Drives the real /auth/register and
        /auth/login endpoints (exactly what production traffic does), then
        confirms a concurrent/prior user's /products query is unaffected.

        Pre-fix: this fails — User A's product query returns the wrong count
        (or 0) immediately after another user registers/logs in, because
        auth.py's sign_up()/sign_in_with_password() calls contaminated the
        shared get_db() singleton.

        Post-fix: this passes — auth.py uses get_auth_client(), a separate
        client, so get_db()'s singleton is never touched by auth operations.
        """
        from api.main import app

        client = TestClient(app, raise_server_exceptions=False)
        throwaway_email = f"qa-singleton-test-{uuid.uuid4().hex[:8]}@example.com"

        try:
            token_a = _get_token_for_user(_USER_A_EMAIL)
            baseline = client.get("/products", headers={"Authorization": f"Bearer {token_a}"})
            assert baseline.status_code == 200
            baseline_products = baseline.json()
            assert len(baseline_products) > 0, (
                "Test precondition failed: seeded user has no products in the "
                "live DB — cannot verify isolation without a non-empty baseline"
            )

            # Simulate a second, concurrent user registering AND logging in —
            # exactly what triggered the contamination in the QA report.
            reg = client.post(
                "/auth/register",
                json={"email": throwaway_email, "password": "ThrowawayPass123!"},
            )
            assert reg.status_code == 201, f"register failed: {reg.status_code} {reg.text}"

            login = client.post(
                "/auth/login",
                json={"email": throwaway_email, "password": "ThrowawayPass123!"},
            )
            assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"

            refresh_token = login.json().get("refresh_token")
            if refresh_token:
                refresh_resp = client.post("/auth/refresh", json={"refresh_token": refresh_token})
                assert refresh_resp.status_code == 200

            # Immediately re-query User A's products on the SAME process/singleton.
            after = client.get("/products", headers={"Authorization": f"Bearer {token_a}"})
            assert after.status_code == 200, (
                f"GET /products for User A returned {after.status_code} after another "
                f"user's register/login/refresh — this is the singleton contamination bug"
            )
            after_products = after.json()
            assert len(after_products) == len(baseline_products), (
                f"TENANT ISOLATION REGRESSION: User A had {len(baseline_products)} "
                f"products before another user registered/logged in, but "
                f"{len(after_products)} immediately after — the shared get_db() "
                f"singleton was contaminated by auth.py's register/login/refresh call."
            )
        finally:
            _delete_user_by_email(throwaway_email)

    def test_get_db_singleton_is_still_inherently_vulnerable_if_misused(self, live_env):
        """
        Documents the underlying danger directly: calling sign_up() on
        get_db() itself (bypassing the fix) still contaminates it — this is
        inherent supabase-py/GoTrue behavior, not something the fix patches
        away. This is why the fix is "never call these methods on get_db()"
        and not "make get_db() immune" — there is no such API. Confirms the
        QA report's root-cause diagnosis remains accurate post-fix, and
        guards against a future regression where someone calls these methods
        on get_db() again.
        """
        from db.client import get_db

        db = get_db()
        throwaway_email = f"qa-singleton-direct-{uuid.uuid4().hex[:8]}@example.com"

        try:
            before = db.table("products").select("id").eq("user_id", _USER_A_ID).execute()
            assert len(before.data) > 0

            try:
                db.auth.sign_up({"email": throwaway_email, "password": "ThrowawayPass123!"})
            except Exception:
                pass  # registration outcome irrelevant — only the session mutation matters

            after = db.table("products").select("id").eq("user_id", _USER_A_ID).execute()
            assert len(after.data) != len(before.data), (
                "Expected calling auth.sign_up() directly on get_db() to still "
                "contaminate the singleton (this is the underlying library "
                "behavior the fix routes around, not something that can be "
                "patched) — if this assertion fails, the supabase-py library's "
                "session-handling behavior has changed and the warning "
                "docstrings in db/client.py may need revisiting."
            )
        finally:
            _delete_user_by_email(throwaway_email)
            get_db.cache_clear()
