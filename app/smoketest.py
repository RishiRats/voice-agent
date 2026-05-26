"""Quick sanity check — verifies your Postgres + tenant loading works.

Run this BEFORE you need your Sarvam API key. If this works, you know your
infrastructure is good and only the LLM/STT/TTS layer is left to wire up.

    python -m app.smoketest
"""
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv
from loguru import logger

load_dotenv()


async def main():
    db_url = os.environ.get("POSTGRES_URL")
    if not db_url:
        logger.error("POSTGRES_URL not set. Copy .env.example to .env.")
        sys.exit(1)

    # 1. Can we reach Postgres?
    try:
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
        logger.success(f"✓ Connected to Postgres at {db_url}")
    except Exception as e:
        logger.error(f"✗ Cannot connect to Postgres: {e}")
        logger.error("  Did you run `podman-compose up -d` ?")
        sys.exit(1)

    # 2. Does the tenants table exist?
    from app.services.tenant_loader import load_tenant_by_id

    tenant_id = int(os.environ.get("DEMO_TENANT_ID", "1"))
    try:
        tenant = await load_tenant_by_id(pool, tenant_id)
    except Exception as e:
        logger.error(f"✗ Cannot load tenant {tenant_id}: {e}")
        logger.error("  Did you run `python -m app.db.migrate` ?")
        sys.exit(1)

    logger.success(f"✓ Loaded tenant: id={tenant.id} name={tenant.name!r}")
    logger.info(f"  Voice: {tenant.voice}")
    logger.info(f"  Language: {tenant.default_language}")
    logger.info(f"  LLM: {tenant.llm_model} (temperature={tenant.temperature})")
    logger.info(f"  Tools enabled: {tenant.tools_enabled}")
    logger.info(f"  System prompt length: {len(tenant.system_prompt)} chars")
    logger.info(f"  System prompt preview: {tenant.system_prompt[:200]!r}...")
    logger.info(f"  Greeting: {tenant.greeting!r}")

    # 3. Is the Sarvam API key set?
    sarvam_key = os.environ.get("SARVAM_API_KEY")
    if not sarvam_key or sarvam_key == "your_key_here":
        logger.warning("⚠ SARVAM_API_KEY not set yet — paste it into .env when you have it.")
        logger.warning("  Once it's set, run: python -m app.main")
    else:
        logger.success(f"✓ SARVAM_API_KEY is set (length={len(sarvam_key)})")
        logger.info("  You're ready to run: python -m app.main")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
