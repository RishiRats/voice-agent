"""Standalone retention enforcement script.

Run via cron (host) or as a one-shot container in Stage 3+. Reports
how many rows were deleted across the relevant tables.

Usage:
    python -m app.db.retention_job
    RETENTION_DAYS=30 python -m app.db.retention_job  # override default
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv
from loguru import logger

load_dotenv()


async def main():
    days = int(os.environ.get("RETENTION_DAYS", "90"))
    db_url = os.environ["POSTGRES_URL"]
    logger.info(f"Running retention enforcement: {days} day window")

    conn = await asyncpg.connect(db_url)
    try:
        results = await conn.fetch("SELECT * FROM enforce_retention($1)", days)
        total = 0
        for row in results:
            logger.info(f"  {row['table_name']}: deleted {row['rows_deleted']} rows")
            total += row["rows_deleted"]
        logger.info(f"Retention complete. Total rows deleted: {total}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
