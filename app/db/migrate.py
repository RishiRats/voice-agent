"""Run schema + incremental migrations + seed against Postgres.

Run with:   python -m app.db.migrate

Idempotent: safe to run repeatedly. All DDL uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
"""
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

SCHEMA_FILE = Path(__file__).parent / "schema.sql"
SEED_FILE = Path(__file__).parent / "seed.sql"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def migrate():
    db_url = os.environ.get("POSTGRES_URL")
    if not db_url:
        logger.error("POSTGRES_URL not set in environment. Copy .env.example to .env first.")
        sys.exit(1)

    logger.info(f"Connecting to {db_url}")
    conn = await asyncpg.connect(db_url)
    try:
        logger.info(f"Applying schema from {SCHEMA_FILE}")
        await conn.execute(SCHEMA_FILE.read_text())

        # Apply numbered migration files in order (all use IF NOT EXISTS — idempotent)
        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for mf in migration_files:
            logger.info(f"Applying migration {mf.name}")
            await conn.execute(mf.read_text())

        logger.info(f"Seeding demo tenant from {SEED_FILE}")
        await conn.execute(SEED_FILE.read_text())

        rows = await conn.fetch("SELECT id, name, inbound_did FROM tenants ORDER BY id")
        logger.info(f"Tenants now in database ({len(rows)}):")
        for r in rows:
            logger.info(f"  id={r['id']}  name={r['name']!r}  did={r['inbound_did']}")

        logger.info("Migration complete.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
