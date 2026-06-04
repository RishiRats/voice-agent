"""FastAPI tools service — Stage 2.

Exposes check_availability and book_appointment as HTTP endpoints so that
Pipecat's LLM function-call handlers can reach them via httpx.

Run:
    python -m app.tools.server
"""
import asyncio
import secrets
from datetime import date, datetime, timedelta, time
from typing import Annotated, Literal

import json

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import config
from app.services.log_redact import redact_phone, redact_name

app = FastAPI(title="Voice Agent Tools")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------------------------------------------------------------------------
# Auth dependency — shared secret header check on all protected endpoints.
# ---------------------------------------------------------------------------

async def require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """Constant-time comparison against the shared secret."""
    if x_internal_token is None:
        raise HTTPException(status_code=401, detail="Missing X-Internal-Token")
    if not secrets.compare_digest(x_internal_token, config.TOOLS_INTERNAL_TOKEN):
        logger.warning(
            "Tools API auth failure — invalid X-Internal-Token. "
            "Source IP available in slowapi rate-limit logs."
        )
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/health")
async def health():
    """Unauthenticated — safe for monitoring/load-balancer health checks."""
    return {"status": "ok"}

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

_DOW_MAP = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _is_open(business_hours: dict, requested_date: date) -> bool:
    dow = _DOW_MAP[requested_date.weekday()]
    if dow in business_hours:
        return business_hours[dow] != "closed"
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


@app.post("/tools/check_availability", response_model=CheckAvailabilityResponse,
          dependencies=[Depends(require_internal_token)])
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
        available_slots=open_slots[:3],
        date=req.date,
    )


# ---------------------------------------------------------------------------
# /tools/book_appointment
# ---------------------------------------------------------------------------

class BookAppointmentRequest(BaseModel):
    tenant_id: int
    call_id: str | None = None
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
    payment_required: bool = False
    payment_amount_paise: int | None = None


@app.post("/tools/book_appointment", response_model=BookAppointmentResponse,
          dependencies=[Depends(require_internal_token)])
@limiter.limit("30/minute")
async def book_appointment(request: Request, req: BookAppointmentRequest):
    slot_dt = datetime.fromisoformat(req.slot)
    pool = await get_pg_pool()

    # Fetch tenant payment config alongside the booking
    tenant_row = await pool.fetchrow(
        "SELECT payment_enabled, payment_amount_paise, payment_expiry_hours FROM tenants WHERE id = $1",
        req.tenant_id,
    )
    if not tenant_row:
        raise HTTPException(status_code=404, detail="tenant not found")

    payment_enabled = tenant_row["payment_enabled"]

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

                if payment_enabled:
                    initial_status = "pending"
                    payment_status = "pending"
                    payment_expires_at = datetime.utcnow() + timedelta(
                        hours=tenant_row["payment_expiry_hours"]
                    )
                else:
                    initial_status = "booked"
                    payment_status = "not_required"
                    payment_expires_at = None

                row = await conn.fetchrow(
                    """
                    INSERT INTO appointments
                      (tenant_id, call_id, caller_name, caller_phone,
                       slot_at, notes, status,
                       payment_status, payment_expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING id, slot_at
                    """,
                    req.tenant_id,
                    req.call_id,
                    req.caller_name,
                    req.caller_phone,
                    slot_dt,
                    req.notes,
                    initial_status,
                    payment_status,
                    payment_expires_at,
                )
    except asyncpg.exceptions.UniqueViolationError:
        return BookAppointmentResponse(success=False, reason="slot_taken")

    logger.info(
        f"APPOINTMENT: id={row['id']} tenant={req.tenant_id} "
        f"name={redact_name(req.caller_name)} phone={redact_phone(req.caller_phone)} "
        f"slot={row['slot_at'].isoformat()} status={initial_status} payment={payment_status}"
    )

    return BookAppointmentResponse(
        success=True,
        appointment_id=row["id"],
        confirmed_slot=row["slot_at"].isoformat(),
        payment_required=payment_enabled,
        payment_amount_paise=tenant_row["payment_amount_paise"],
    )


# ---------------------------------------------------------------------------
# /tools/check_trusted_caller
# ---------------------------------------------------------------------------

class CheckTrustedRequest(BaseModel):
    tenant_id: int
    caller_phone: str


class CheckTrustedResponse(BaseModel):
    is_trusted: bool


@app.post("/tools/check_trusted_caller", response_model=CheckTrustedResponse,
          dependencies=[Depends(require_internal_token)])
@limiter.limit("60/minute")
async def check_trusted_caller(request: Request, body: CheckTrustedRequest):
    pool = await get_pg_pool()
    row = await pool.fetchval(
        "SELECT id FROM trusted_callers WHERE tenant_id = $1 AND phone = $2",
        body.tenant_id,
        body.caller_phone,
    )
    return CheckTrustedResponse(is_trusted=row is not None)


# ---------------------------------------------------------------------------
# /tools/confirm_payment
# ---------------------------------------------------------------------------

class ConfirmPaymentRequest(BaseModel):
    appointment_id: int
    tenant_id: int
    payment_success: bool


class ConfirmPaymentResponse(BaseModel):
    appointment_status: str
    payment_status: str


@app.post("/tools/confirm_payment", response_model=ConfirmPaymentResponse,
          dependencies=[Depends(require_internal_token)])
@limiter.limit("30/minute")
async def confirm_payment(request: Request, body: ConfirmPaymentRequest):
    pool = await get_pg_pool()
    if body.payment_success:
        await pool.execute(
            """
            UPDATE appointments
            SET status = 'booked',
                payment_status = 'paid',
                payment_completed_at = NOW()
            WHERE id = $1 AND tenant_id = $2
            """,
            body.appointment_id,
            body.tenant_id,
        )
        logger.info(
            f"Payment confirmed: appointment_id={body.appointment_id} tenant={body.tenant_id}"
        )
        return ConfirmPaymentResponse(appointment_status="booked", payment_status="paid")
    else:
        logger.info(
            f"Payment failed: appointment_id={body.appointment_id} tenant={body.tenant_id}"
        )
        return ConfirmPaymentResponse(appointment_status="pending", payment_status="failed")


# ---------------------------------------------------------------------------
# /tools/create_handoff_request
# ---------------------------------------------------------------------------

class HandoffRequestBody(BaseModel):
    tenant_id: int
    call_id: str
    appointment_id: int | None = None
    reason: str
    urgency: str = "normal"
    caller_phone: str | None = None


@app.post("/tools/create_handoff_request",
          dependencies=[Depends(require_internal_token)])
@limiter.limit("30/minute")
async def create_handoff_request(request: Request, body: HandoffRequestBody):
    pool = await get_pg_pool()
    await pool.execute(
        """
        INSERT INTO handoff_requests
          (tenant_id, call_id, appointment_id, reason, urgency, caller_phone)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        body.tenant_id,
        body.call_id,
        body.appointment_id,
        body.reason,
        body.urgency,
        body.caller_phone,
    )
    logger.info(
        f"Handoff created: tenant={body.tenant_id} reason={body.reason!r} urgency={body.urgency}"
    )
    return {"created": True}


# ---------------------------------------------------------------------------
# /tools/cancel_expired_payments  — called by the retention job
# ---------------------------------------------------------------------------

@app.post("/tools/cancel_expired_payments",
          dependencies=[Depends(require_internal_token)])
async def cancel_expired_payments(request: Request):
    pool = await get_pg_pool()
    result = await pool.fetch(
        """
        UPDATE appointments
        SET status = 'cancelled',
            payment_status = 'expired'
        WHERE payment_status = 'pending'
          AND payment_expires_at < NOW()
          AND status != 'cancelled'
        RETURNING id, tenant_id
        """
    )
    cancelled = [{"id": r["id"], "tenant_id": r["tenant_id"]} for r in result]
    logger.info(f"Auto-cancelled {len(cancelled)} expired pending appointments")
    return {"cancelled_count": len(cancelled), "cancelled": cancelled}


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
