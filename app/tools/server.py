"""FastAPI tools service — Stage 2.

Exposes check_availability and book_appointment as HTTP endpoints so that
Pipecat's LLM function-call handlers can reach them via httpx.

Run:
    python -m app.tools.server
"""
import asyncio
from datetime import date, datetime, timedelta, time
from typing import Literal

import json

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import config

app = FastAPI(title="Voice Agent Tools")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Lazy Postgres pool — same pattern as main.py
# ---------------------------------------------------------------------------

_pg_pool: asyncpg.Pool | None = None


async def get_pg_pool() -> asyncpg.Pool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = await asyncpg.create_pool(
            config.POSTGRES_URL,
            min_size=2,
            max_size=10,
            command_timeout=10,
        )
    return _pg_pool


@app.on_event("shutdown")
async def shutdown():
    global _pg_pool
    if _pg_pool:
        await _pg_pool.close()


# ---------------------------------------------------------------------------
# Slot generation helpers
# ---------------------------------------------------------------------------

TIME_RANGES = {
    "morning":   (time(10, 0), time(13, 0)),
    "afternoon": (time(13, 0), time(16, 0)),
    "evening":   (time(16, 0), time(20, 0)),
    "any":       (time(10, 0), time(20, 0)),
}

# Day-of-week index (Monday=0) → canonical abbreviated name
_DOW_MAP = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _is_open(business_hours: dict, requested_date: date) -> bool:
    """Return True if the tenant is open on requested_date."""
    dow = _DOW_MAP[requested_date.weekday()]  # e.g. "sun"
    # Check explicit single-day key first
    if dow in business_hours:
        return business_hours[dow] != "closed"
    # Check range keys like "mon-sat"
    for key, value in business_hours.items():
        if "-" in key:
            start_day, end_day = key.split("-", 1)
            if start_day in _DOW_MAP and end_day in _DOW_MAP:
                start_idx = _DOW_MAP.index(start_day)
                end_idx = _DOW_MAP.index(end_day)
                dow_idx = _DOW_MAP.index(dow)
                if start_idx <= dow_idx <= end_idx:
                    return value != "closed"
    return False


def _generate_slots(range_start: time, range_end: time) -> list[time]:
    """30-minute slots within [range_start, range_end)."""
    slots = []
    current = datetime.combine(date.today(), range_start)
    end = datetime.combine(date.today(), range_end)
    while current < end:
        slots.append(current.time())
        current += timedelta(minutes=30)
    return slots


# ---------------------------------------------------------------------------
# /tools/check_availability
# ---------------------------------------------------------------------------

class CheckAvailabilityRequest(BaseModel):
    tenant_id: int
    date: str  # YYYY-MM-DD
    time_range: Literal["morning", "afternoon", "evening", "any"]

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD")
        return v


class CheckAvailabilityResponse(BaseModel):
    available_slots: list[str]
    date: str | None = None
    error: str | None = None


@app.post("/tools/check_availability", response_model=CheckAvailabilityResponse)
@limiter.limit("60/minute")
async def check_availability(request: Request, req: CheckAvailabilityRequest):
    today = date.today()
    requested = datetime.strptime(req.date, "%Y-%m-%d").date()

    if requested < today or requested > today + timedelta(days=30):
        return CheckAvailabilityResponse(available_slots=[], error="out_of_range")

    pool = await get_pg_pool()
    row = await pool.fetchrow(
        "SELECT business_hours FROM tenants WHERE id = $1", req.tenant_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="tenant not found")

    bh_raw = row["business_hours"]
    business_hours: dict = json.loads(bh_raw) if isinstance(bh_raw, str) else bh_raw
    if not _is_open(business_hours, requested):
        return CheckAvailabilityResponse(available_slots=[], error="closed")

    range_start, range_end = TIME_RANGES[req.time_range]
    all_slots = _generate_slots(range_start, range_end)

    # Fetch already-booked slots for this tenant + date
    booked_rows = await pool.fetch(
        """
        SELECT slot_at FROM appointments
        WHERE tenant_id = $1
          AND slot_at::date = $2
          AND status = 'booked'
        """,
        req.tenant_id,
        requested,
    )
    booked_times = {r["slot_at"].time().replace(second=0, microsecond=0) for r in booked_rows}

    open_slots = [
        s.strftime("%H:%M")
        for s in all_slots
        if s not in booked_times
    ]

    return CheckAvailabilityResponse(
        available_slots=open_slots[:4],
        date=req.date,
    )


# ---------------------------------------------------------------------------
# /tools/book_appointment
# ---------------------------------------------------------------------------

class BookAppointmentRequest(BaseModel):
    tenant_id: int
    call_id: str | None = None  # NULL until call_log row exists; FK is nullable
    slot: str  # ISO datetime e.g. "2026-05-28T11:30:00"
    caller_name: str
    caller_phone: str
    notes: str | None = None

    @field_validator("slot")
    @classmethod
    def validate_slot(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("slot must be ISO datetime e.g. 2026-05-28T11:30:00")
        return v


class BookAppointmentResponse(BaseModel):
    success: bool
    appointment_id: int | None = None
    confirmed_slot: str | None = None
    reason: str | None = None


@app.post("/tools/book_appointment", response_model=BookAppointmentResponse)
@limiter.limit("30/minute")
async def book_appointment(request: Request, req: BookAppointmentRequest):
    slot_dt = datetime.fromisoformat(req.slot)
    pool = await get_pg_pool()

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchval(
                    """
                    SELECT id FROM appointments
                    WHERE tenant_id = $1 AND slot_at = $2 AND status = 'booked'
                    FOR UPDATE
                    """,
                    req.tenant_id,
                    slot_dt,
                )
                if existing is not None:
                    return BookAppointmentResponse(success=False, reason="slot_taken")

                row = await conn.fetchrow(
                    """
                    INSERT INTO appointments (tenant_id, call_id, caller_name, caller_phone, slot_at, notes, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'booked')
                    RETURNING id, slot_at
                    """,
                    req.tenant_id,
                    req.call_id,
                    req.caller_name,
                    req.caller_phone,
                    slot_dt,
                    req.notes,
                )
    except asyncpg.exceptions.UniqueViolationError:
        return BookAppointmentResponse(success=False, reason="slot_taken")

    logger.info(
        f"BOOKED: id={row['id']} tenant={req.tenant_id} "
        f"{req.caller_name} {req.caller_phone} {row['slot_at'].isoformat()}"
    )

    return BookAppointmentResponse(
        success=True,
        appointment_id=row["id"],
        confirmed_slot=row["slot_at"].isoformat(),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from app import config
    import uvicorn
    uvicorn.run(
        "app.tools.server:app",
        host=config.TOOLS_HOST,
        port=config.TOOLS_PORT,
        reload=False,
    )
