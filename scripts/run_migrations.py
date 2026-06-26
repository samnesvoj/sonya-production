"""
run_migrations.py
=================
Applies all SQL migrations in scripts/migrations/ in order.
Run once on deploy: python scripts/run_migrations.py

Safe to run multiple times — uses IF NOT EXISTS and ON CONFLICT.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set")

    try:
        import psycopg2
    except ImportError:
        raise SystemExit("psycopg2 not installed — run: pip install psycopg2-binary")

    migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        raise SystemExit(f"No migration files found in {_MIGRATIONS_DIR}")

    conn = psycopg2.connect(url)
    try:
        for migration in migration_files:
            sql = migration.read_text(encoding="utf-8")
            logger.info("Applying migration: %s", migration.name)
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                logger.info("  OK: %s", migration.name)
            except Exception as exc:
                logger.error("  FAILED: %s — %s", migration.name, exc)
                raise SystemExit(f"Migration failed: {migration.name}: {exc}")
    finally:
        conn.close()

    logger.info("All %d migrations applied successfully.", len(migration_files))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
