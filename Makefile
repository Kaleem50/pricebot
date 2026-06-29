# ============================================================
# PriceBot Makefile
#
# Common dev commands. Copy .env.example to .env and fill in
# all values before running any target.
#
# Prerequisites:
#   pip install -r requirements.txt
#   SUPABASE_DB_URL=postgresql://postgres:<pass>@db.<ref>.supabase.co:5432/postgres
# ============================================================

.PHONY: dev test migrate migrate-status lint format

# Start the API server in hot-reload mode
dev:
	uvicorn api.main:app --reload --port 8000 --host 0.0.0.0

# Run all unit and integration tests with verbose output
test:
	pytest tests/ -v

# Apply all pending migration files to the Supabase database in order.
# Requires SUPABASE_DB_URL to be set in .env or the shell environment.
migrate:
	python3 db/migrate.py

migrate-status:
	python3 db/migrate.py --status

# Lint Python source with ruff (fast, PEP 8 compliant)
lint:
	ruff check .

# Auto-format Python source with black
format:
	black .
