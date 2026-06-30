"""
db/migrate.py — PriceBot Database Migration Runner

Applies SQL migration files from db/migrations/ in filename order (001_, 002_, …).
Tracks applied migrations in a ``migrations_log`` table so re-runs are safe.

Connection strategy (tried in order):
  1. psycopg2 direct  — SUPABASE_DB_URL  (db.<ref>.supabase.co:5432)
  2. psycopg2 pooler  — auto-derived from SUPABASE_DB_URL
                        (aws-0-<region>.pooler.supabase.com:6543)
  3. HTTPS Management API — SUPABASE_PAT + ref from SUPABASE_URL
                            (api.supabase.com/v1/projects/<ref>/database/query)

Usage:
    python db/migrate.py              # apply all pending migrations
    python db/migrate.py --status     # list applied / pending (needs a live connection)
    python db/migrate.py --print-sql  # dump all pending SQL to stdout (no DB required)

Required in .env:
    SUPABASE_DB_URL   — postgresql://postgres:<pass>@db.<ref>.supabase.co:5432/postgres
    SUPABASE_URL      — https://<ref>.supabase.co
    SUPABASE_PAT      — personal access token from supabase.com/dashboard/account/tokens
                        (only needed when direct/pooler TCP is blocked)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import urllib.parse
from pathlib import Path

import sqlparse
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate")

try:
    import psycopg2
    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False
    logger.warning("psycopg2 not installed — TCP connections unavailable.")

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POOLER_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-central-1", "eu-west-1", "eu-west-2",
    "ap-southeast-1", "ap-northeast-1", "ap-south-1",
    "ca-central-1", "sa-east-1",
]

_MGMT_API_BASE = "https://api.supabase.com/v1"

_EXPECTED_TABLES = [
    "subscriptions",
    "platform_connections",
    "products",
    "repricing_jobs",
    "price_history",
    "batch_results",
    "usage_events",
]

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS migrations_log (
    id          SERIAL PRIMARY KEY,
    filename    TEXT        NOT NULL UNIQUE,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


# ---------------------------------------------------------------------------
# psycopg2 helpers
# ---------------------------------------------------------------------------


def _extract_ref_from_db_url(direct_url: str) -> str | None:
    """Extract the Supabase project ref from a direct DB URL hostname."""
    parsed = urllib.parse.urlparse(direct_url)
    m = re.match(r"^db\.([^.]+)\.supabase\.co$", parsed.hostname or "")
    return m.group(1) if m else None


def _extract_ref_from_api_url(api_url: str) -> str | None:
    """Extract the Supabase project ref from the SUPABASE_URL API hostname."""
    parsed = urllib.parse.urlparse(api_url)
    m = re.match(r"^([^.]+)\.supabase\.co$", parsed.hostname or "")
    return m.group(1) if m else None


def _pooler_candidates(direct_url: str) -> list[str]:
    """Derive all pooler candidate URLs from the direct DB URL."""
    ref = _extract_ref_from_db_url(direct_url)
    if not ref:
        return []
    parsed = urllib.parse.urlparse(direct_url)
    password = urllib.parse.unquote(parsed.password or "")
    encoded_pw = urllib.parse.quote(password, safe="")
    return [
        f"postgresql://postgres.{ref}:{encoded_pw}"
        f"@aws-0-{region}.pooler.supabase.com:6543/postgres?sslmode=require"
        for region in _POOLER_REGIONS
    ]


def _try_psycopg2(url: str, timeout: int = 5) -> "psycopg2.connection | None":
    """Try a psycopg2 connection; return the connection or None on any error."""
    if not _HAS_PSYCOPG2:
        return None
    try:
        conn = psycopg2.connect(url, connect_timeout=timeout)
        conn.autocommit = True
        return conn
    except Exception:
        return None


def _open_psycopg2(direct_url: str) -> "psycopg2.connection | None":
    """
    Try the direct URL then each pooler region.

    Returns a live connection, or None if every attempt fails.
    """
    logger.info("Trying direct PostgreSQL connection …")
    conn = _try_psycopg2(direct_url)
    if conn:
        logger.info("Connected via direct URL.")
        return conn

    logger.info("Direct failed. Trying connection pooler (%d regions) …", len(_POOLER_REGIONS))
    for candidate in _pooler_candidates(direct_url):
        m = re.search(r"aws-0-([^.]+)", candidate)
        region = m.group(1) if m else "?"
        conn = _try_psycopg2(candidate, timeout=5)
        if conn:
            logger.info("Connected via pooler  region=%s", region)
            return conn

    return None


# ---------------------------------------------------------------------------
# Management API helpers (HTTPS — works even when all TCP ports are blocked)
# ---------------------------------------------------------------------------


def _mgmt_ref(db_url: str, api_url: str) -> str | None:
    """Return the project ref, preferring SUPABASE_URL over SUPABASE_DB_URL."""
    return _extract_ref_from_api_url(api_url) or _extract_ref_from_db_url(db_url)


def _mgmt_execute(ref: str, pat: str, sql: str) -> dict:
    """
    Execute a SQL statement via the Supabase Management API.

    Args:
        ref: Supabase project ref (e.g. ``nacuouuiligdpurmzwom``).
        pat: Personal access token from supabase.com/dashboard/account/tokens.
        sql: SQL statement to execute.

    Returns:
        Parsed JSON response body.

    Raises:
        RuntimeError: If the API returns a non-2xx status.
    """
    if not _HAS_HTTPX:
        raise RuntimeError("httpx not installed. Run: pip install httpx")

    url = f"{_MGMT_API_BASE}/projects/{ref}/database/query"
    resp = httpx.post(
        url,
        json={"query": sql},
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json"},
        timeout=30,
    )
    if not resp.is_success:
        raise RuntimeError(f"Management API error {resp.status_code}: {resp.text[:400]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Adapter: uniform interface over psycopg2 or Management API
# ---------------------------------------------------------------------------


class _PsycoAdapter:
    """Wraps a psycopg2 connection to match the adapter interface."""

    def __init__(self, conn: "psycopg2.connection") -> None:  # noqa: F821
        self._conn = conn
        self._cur = conn.cursor()

    def execute(self, sql: str) -> list[tuple]:
        self._cur.execute(sql)
        try:
            return self._cur.fetchall()
        except psycopg2.ProgrammingError:
            return []

    def execute_param(self, sql: str, params: tuple) -> None:
        self._cur.execute(sql, params)

    def close(self) -> None:
        self._cur.close()
        self._conn.close()


class _MgmtAdapter:
    """Wraps the Management API to match the adapter interface."""

    def __init__(self, ref: str, pat: str) -> None:
        self._ref = ref
        self._pat = pat

    def execute(self, sql: str) -> list[tuple]:
        result = _mgmt_execute(self._ref, self._pat, sql)
        if isinstance(result, list):
            return [tuple(row.values()) for row in result]
        return []

    def execute_param(self, sql: str, params: tuple) -> None:
        # Management API does not support parameterised queries — inline safely.
        # Only called with trusted internal values (migration filenames).
        safe_params = tuple(str(p).replace("'", "''") for p in params)
        formatted = sql.replace("%s", "'%s'") % safe_params
        _mgmt_execute(self._ref, self._pat, formatted)

    def close(self) -> None:
        pass


def _open_connection(
    db_url: str,
    api_url: str,
    pat: str,
) -> "_PsycoAdapter | _MgmtAdapter":
    """
    Open the best available database connection.

    Priority: psycopg2 (direct → pooler) → Management API (HTTPS).

    Raises:
        SystemExit: If no connection method is available.
    """
    conn = _open_psycopg2(db_url)
    if conn:
        return _PsycoAdapter(conn)

    if pat:
        ref = _mgmt_ref(db_url, api_url)
        if not ref:
            logger.error(
                "Cannot derive project ref from SUPABASE_URL or SUPABASE_DB_URL."
            )
            sys.exit(1)
        logger.info(
            "TCP connections blocked. Using Supabase Management API (HTTPS)  ref=%s", ref
        )
        # Quick connectivity test
        try:
            _mgmt_execute(ref, pat, "SELECT 1;")
            logger.info("Management API connection verified.")
            return _MgmtAdapter(ref, pat)
        except Exception as exc:
            logger.error("Management API connection failed: %s", exc)
            logger.error(
                "Ensure SUPABASE_PAT is a valid token from "
                "supabase.com/dashboard/account/tokens"
            )
            sys.exit(1)

    logger.error(
        "\nAll connection attempts failed.\n\n"
        "Supabase TCP ports (5432, 6543) are blocked from this machine.\n"
        "To fix, add a Personal Access Token to your .env:\n\n"
        "    SUPABASE_PAT=sbp_...  (from supabase.com/dashboard/account/tokens)\n\n"
        "Or run the migrations manually via the Supabase Dashboard SQL Editor:\n"
        "    https://supabase.com/dashboard/project/%s/sql/new\n\n"
        "Tip:  python db/migrate.py --print-sql  will print the SQL to paste.",
        _mgmt_ref(db_url, api_url) or "<ref>",
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Core migration logic
# ---------------------------------------------------------------------------


def _get_applied(adapter: "_PsycoAdapter | _MgmtAdapter") -> set[str]:
    """Return filenames already present in migrations_log."""
    rows = adapter.execute("SELECT filename FROM migrations_log ORDER BY filename;")
    return {row[0] for row in rows}


def _run_statements(
    adapter: "_PsycoAdapter | _MgmtAdapter",
    sql: str,
    filename: str,
) -> bool:
    """
    Execute a SQL migration file statement-by-statement.

    pg_cron statements are non-fatal (skip with warning).
    All other errors abort the migration.

    Returns:
        True on full success; False on hard failure.
    """
    statements = [s.strip() for s in sqlparse.split(sql) if s.strip()]
    applied = skipped = 0

    for stmt in statements:
        is_cron = "cron." in stmt.lower() or "pg_cron" in stmt.lower()
        try:
            adapter.execute(stmt)
            applied += 1
        except Exception as exc:
            err_str = str(exc).lower()
            is_duplicate = any(kw in err_str for kw in (
                "already exists", "duplicate", "duplicateobject",
            ))
            if is_duplicate:
                skipped += 1
            elif is_cron:
                logger.warning(
                    "  [skip]  pg_cron statement skipped "
                    "(enable pg_cron in Supabase Dashboard → Database → Extensions): %s",
                    str(exc)[:120],
                )
                skipped += 1
            else:
                logger.error("  [FAIL]  %s", str(exc)[:300])
                logger.error("  SQL:    %.200s", stmt)
                return False

    logger.info("  → %d statement(s) executed, %d skipped.", applied, skipped)
    return True


def _verify_tables(adapter: "_PsycoAdapter | _MgmtAdapter") -> None:
    """Log presence/absence of each of the 7 expected tables."""
    rows = adapter.execute(
        f"""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ({', '.join(f"'{t}'" for t in _EXPECTED_TABLES)})
        ORDER BY table_name;
        """
    )
    found = {row[0] for row in rows}
    logger.info("Table verification:")
    for name in sorted(_EXPECTED_TABLES):
        mark = "✓" if name in found else "✗ MISSING"
        logger.info("  %s  %s", mark, name)
    if found == set(_EXPECTED_TABLES):
        logger.info("All 7 tables confirmed.")
    else:
        logger.warning("Missing tables: %s", sorted(set(_EXPECTED_TABLES) - found))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and drive the migration process."""
    parser = argparse.ArgumentParser(description="PriceBot DB migration runner")
    parser.add_argument("--status", action="store_true", help="Show migration status and exit.")
    parser.add_argument(
        "--print-sql",
        action="store_true",
        help="Print all pending SQL to stdout (no DB connection needed).",
    )
    args = parser.parse_args()

    db_url = os.environ.get("SUPABASE_DB_URL", "").strip()
    api_url = os.environ.get("SUPABASE_URL", "").strip()
    pat = os.environ.get("SUPABASE_PAT", "").strip()

    if not db_url:
        logger.error("SUPABASE_DB_URL is not set in .env")
        sys.exit(1)

    migrations_dir = Path(__file__).resolve().parent / "migrations"
    migration_files = sorted(migrations_dir.glob("*.sql"))

    if not migration_files:
        logger.warning("No .sql files found in %s", migrations_dir)
        return

    # ── Print-SQL mode (no DB needed) ────────────────────────────────────────
    if args.print_sql:
        print("-- ============================================================")
        print("-- PriceBot migration SQL — paste into Supabase Dashboard SQL Editor")
        print("-- https://supabase.com/dashboard/project/%s/sql/new" %
              (_mgmt_ref(db_url, api_url) or "YOUR_PROJECT_REF"))
        print("-- ============================================================")
        print()
        print(_BOOTSTRAP_SQL.strip())
        print()
        for path in migration_files:
            print(f"-- === {path.name} ===")
            print(path.read_text().strip())
            print()
        return

    # ── All other modes need a live DB connection ─────────────────────────────
    adapter = _open_connection(db_url, api_url, pat)

    # Bootstrap migrations_log
    adapter.execute(_BOOTSTRAP_SQL)
    applied_set = _get_applied(adapter)

    # ── Status mode ──────────────────────────────────────────────────────────
    if args.status:
        print(f"\n  {'Migration':<52} {'Status':>10}")
        print("  " + "-" * 64)
        for f in migration_files:
            status = "applied" if f.name in applied_set else "PENDING"
            print(f"  {f.name:<52} {status:>10}")
        print()
        _verify_tables(adapter)
        adapter.close()
        return

    # ── Apply pending migrations ──────────────────────────────────────────────
    pending = [f for f in migration_files if f.name not in applied_set]

    if not pending:
        logger.info("All %d migration(s) already applied — nothing to do.", len(migration_files))
        _verify_tables(adapter)
        adapter.close()
        return

    logger.info("%d migration(s) pending: %s", len(pending), [f.name for f in pending])

    for path in pending:
        logger.info("Applying  %s …", path.name)
        ok = _run_statements(adapter, path.read_text(), path.name)
        if not ok:
            logger.error("Stopping — migration %s failed.", path.name)
            adapter.close()
            sys.exit(1)
        adapter.execute_param(
            "INSERT INTO migrations_log (filename) VALUES (%s) ON CONFLICT DO NOTHING;",
            (path.name,),
        )
        logger.info("  ✓  %s recorded in migrations_log.", path.name)

    _verify_tables(adapter)
    adapter.close()
    logger.info("Done — %d migration(s) applied.", len(pending))


if __name__ == "__main__":
    main()
