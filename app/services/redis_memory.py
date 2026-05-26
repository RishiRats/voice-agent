"""Redis-backed conversation turn storage — Stage 2.

Turns are stored as a Redis list: call:{call_id}:messages
Each element is a JSON string: {"role": "user"|"assistant", "content": "...", "at": "ISO"}
The key expires after 1 hour (covers long calls + short post-call window).

Usage:
    await append_turns_bulk(call_id, context.messages)  # called at disconnect
"""
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app import config

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


async def append_turns_bulk(call_id: str, messages: list[dict]) -> None:
    """Write all user/assistant turns to Redis in one pipeline call."""
    turns = [
        m for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not turns:
        return

    r = await get_redis()
    key = f"call:{call_id}:messages"
    ts = datetime.now(timezone.utc).isoformat()

    async with r.pipeline(transaction=False) as pipe:
        for m in turns:
            pipe.rpush(key, json.dumps({"role": m["role"], "content": m["content"], "at": ts}))
        pipe.expire(key, 3600)
        await pipe.execute()


async def load_history(call_id: str) -> list[dict]:
    r = await get_redis()
    raw = await r.lrange(f"call:{call_id}:messages", 0, -1)
    return [json.loads(item) for item in raw]
