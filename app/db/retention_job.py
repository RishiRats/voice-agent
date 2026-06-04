"""Standalone retention enforcement script.

Run via cron (host) or as a one-shot container in Stage 3+. Reports
how many rows were deleted across the relevant tables. Also cancels
appointments where the payment link has expired.

Usage:
    python -m app.db.retention_job
    RETENTION_DAYS=30 python -m app.db.retention_job  # override default
"""
import asyncio
import os

import asyncpg
import httpx
from dotenv import load_dotenv
from loguru import logger

load_dotenv()


async def cancel_expired_payments() -> int:
    """Cancel appointments where the payment link has expired."""
    tools_url = os.environ.get(
        "TOOLS_BASE_URL",
        f"http://{os.environ.get('TOOLS_HOST', '127.0.0.1')}:{os.environ.get('TOOLS_PORT', '8000')}",
    )
    token = os.environ.get("TOOLS_INTERNAL_TOKEN", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{tools_url}/tools/cancel_expired_payments",
                headers={"X-Internal-Token": token},
            )
            result = r.json()
            count = result.get("cancelled_count", 0)
            logger.info(f"  expired_payments: cancelled {count} pending appointments")
            return count
    except Exception as e:
        logger.warning(f"  expired_payments: failed to reach tools server — {e}")
        return 0


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

    await cancel_expired_payments()


if __name__ == "__main__":
    asyncio.run(main())
