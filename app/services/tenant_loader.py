"""Load a tenant's full config from Postgres.

In Stage 1 we look up by ID (DEMO_TENANT_ID env var) because there's no real call yet.
In Stage 3 we'll look up by `inbound_did` (the phone number that was dialed).

This is intentionally a *one-shot fetch at call start*. We don't keep a long-lived
DB connection per call — it's fetched once, the dict lives in memory for the call's
lifetime, and the connection goes back to the pool. If a tenant updates their
system_prompt mid-call, the in-flight call keeps the old prompt (consistent), and
the next call picks up the new one. That's the behaviour you want.
"""
import json
from dataclasses import dataclass
from typing import Optional

import asyncpg


@dataclass
class Tenant:
    """Everything the Pipecat pipeline needs to know about a tenant."""
    id: int
    name: str
    inbound_did: Optional[str]
    system_prompt: str
    greeting: str
    voice: str
    default_language: str
    llm_model: str
    temperature: float
    tools_enabled: list[str]
    business_hours: dict
    metadata: dict


async def load_tenant_by_id(pool: asyncpg.Pool, tenant_id: int) -> Tenant:
    """Load tenant config by primary key. Used in Stage 1 for browser-mic testing."""
    row = await pool.fetchrow(
        """
        SELECT id, name, inbound_did, system_prompt, greeting, voice,
               default_language, llm_model, temperature, tools_enabled,
               business_hours, metadata
        FROM tenants
        WHERE id = $1
        """,
        tenant_id,
    )
    if row is None:
        raise ValueError(f"Tenant {tenant_id} not found. Did you run migrations?")
    return _row_to_tenant(row)


async def load_tenant_by_did(pool: asyncpg.Pool, did: str) -> Tenant:
    """Load tenant config by inbound phone number. Used in Stage 3 for real PSTN calls."""
    row = await pool.fetchrow(
        """
        SELECT id, name, inbound_did, system_prompt, greeting, voice,
               default_language, llm_model, temperature, tools_enabled,
               business_hours, metadata
        FROM tenants
        WHERE inbound_did = $1
        """,
        did,
    )
    if row is None:
        raise ValueError(f"No tenant configured for inbound DID {did}")
    return _row_to_tenant(row)


def _row_to_tenant(row: asyncpg.Record) -> Tenant:
    return Tenant(
        id=row["id"],
        name=row["name"],
        inbound_did=row["inbound_did"],
        system_prompt=row["system_prompt"],
        greeting=row["greeting"],
        voice=row["voice"],
        default_language=row["default_language"],
        llm_model=row["llm_model"],
        temperature=float(row["temperature"]),
        tools_enabled=list(_j(row["tools_enabled"])),
        business_hours=dict(_j(row["business_hours"])),
        metadata=dict(_j(row["metadata"])),
    )


def _j(val):
    """Parse a value that may be a JSON string or already a Python object."""
    return json.loads(val) if isinstance(val, str) else val
