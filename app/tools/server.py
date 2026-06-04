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
    service_name: str | None = None
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
    service_name: str | None = None
    service_duration_mins: int = 30


@app.post("/tools/book_appointment", response_model=BookAppointmentResponse,
          dependencies=[Depends(require_internal_token)])
@limiter.limit("30/minute")
async def book_appointment(request: Request, req: BookAppointmentRequest):
    slot_dt = datetime.fromisoformat(req.slot)
    pool = await get_pg_pool()

    # Fetch tenant payment config
    tenant_row = await pool.fetchrow(
        "SELECT payment_enabled, payment_amount_paise, payment_expiry_hours FROM tenants WHERE id = $1",
        req.tenant_id,
    )
    if not tenant_row:
        raise HTTPException(status_code=404, detail="tenant not found")

    payment_enabled = tenant_row["payment_enabled"]

    # Look up service from catalog — drives duration and payment amount
    service_item = None
    if req.service_name:
        service_item = await pool.fetchrow(
            """
            SELECT id, name, price_min_paise, price_max_paise, duration_mins
            FROM catalog_items
            WHERE tenant_id = $1 AND available = true AND name ILIKE $2
            LIMIT 1
            """,
            req.tenant_id,
            req.service_name,
        )
        if not service_item:
            logger.warning(
                f"Service not found in catalog: {req.service_name!r} "
                f"tenant={req.tenant_id} — proceeding with default 30min slot"
            )

    duration_mins = service_item["duration_mins"] if service_item else 30
    payment_amount = (
        service_item["price_min_paise"]
        if service_item and service_item["price_min_paise"] is not None
        else tenant_row["payment_amount_paise"]
    )

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
                       payment_status, payment_expires_at,
                       service_name, service_duration_mins)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
                    req.service_name,
                    duration_mins,
                )
    except asyncpg.exceptions.UniqueViolationError:
        return BookAppointmentResponse(success=False, reason="slot_taken")

    logger.info(
        f"APPOINTMENT: id={row['id']} tenant={req.tenant_id} "
        f"name={redact_name(req.caller_name)} phone={redact_phone(req.caller_phone)} "
        f"slot={row['slot_at'].isoformat()} status={initial_status} payment={payment_status} "
        f"service={req.service_name!r} duration={duration_mins}min"
    )

    return BookAppointmentResponse(
        success=True,
        appointment_id=row["id"],
        confirmed_slot=row["slot_at"].isoformat(),
        payment_required=payment_enabled,
        payment_amount_paise=payment_amount,
        service_name=req.service_name,
        service_duration_mins=duration_mins,
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
        await pool.execute(
            "UPDATE appointments SET payment_status = 'failed' WHERE id = $1 AND tenant_id = $2",
            body.appointment_id,
            body.tenant_id,
        )
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
# Catalog admin CRUD — scoped to tenant (cross-tenant protection enforced)
# ---------------------------------------------------------------------------

# Allowed update fields — used to prevent dynamic SQL injection via field names
_CATALOG_UPDATE_FIELDS = frozenset({
    "name", "description", "category",
    "price_min_paise", "price_max_paise",
    "duration_mins", "available", "display_order",
})


class CatalogItemCreate(BaseModel):
    tenant_id: int
    name: str
    description: str | None = None
    category: str = "General"
    price_min_paise: int | None = None
    price_max_paise: int | None = None
    duration_mins: int = 30
    display_order: int = 0

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name cannot be empty")
        return v.strip()

    @field_validator("price_min_paise", "price_max_paise", mode="before")
    @classmethod
    def price_non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("price cannot be negative")
        return v

    @field_validator("duration_mins")
    @classmethod
    def duration_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("duration must be positive")
        return v


class CatalogItemUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    price_min_paise: int | None = None
    price_max_paise: int | None = None
    duration_mins: int | None = None
    available: bool | None = None
    display_order: int | None = None


@app.get("/admin/catalog",
         dependencies=[Depends(require_internal_token)])
@limiter.limit("60/minute")
async def list_catalog(request: Request, tenant_id: int):
    pool = await get_pg_pool()
    rows = await pool.fetch(
        """
        SELECT id, name, description, category,
               price_min_paise, price_max_paise,
               duration_mins, available, display_order,
               created_at, updated_at
        FROM catalog_items
        WHERE tenant_id = $1
        ORDER BY category, display_order, name
        """,
        tenant_id,
    )
    return {"items": [dict(r) for r in rows]}


@app.post("/admin/catalog",
          dependencies=[Depends(require_internal_token)])
@limiter.limit("30/minute")
async def create_catalog_item(request: Request, body: CatalogItemCreate):
    if (body.price_min_paise is not None and
            body.price_max_paise is not None and
            body.price_max_paise < body.price_min_paise):
        raise HTTPException(
            status_code=422,
            detail="price_max_paise must be >= price_min_paise",
        )
    pool = await get_pg_pool()
    item_id = await pool.fetchval(
        """
        INSERT INTO catalog_items
          (tenant_id, name, description, category,
           price_min_paise, price_max_paise,
           duration_mins, display_order)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        RETURNING id
        """,
        body.tenant_id, body.name, body.description,
        body.category, body.price_min_paise, body.price_max_paise,
        body.duration_mins, body.display_order,
    )
    logger.info(f"Catalog item created: id={item_id} tenant={body.tenant_id} name={body.name!r}")
    return {"created": True, "id": item_id}


@app.patch("/admin/catalog/{item_id}",
           dependencies=[Depends(require_internal_token)])
@limiter.limit("30/minute")
async def update_catalog_item(
    request: Request,
    item_id: int,
    tenant_id: int,
    body: CatalogItemUpdate,
):
    pool = await get_pg_pool()
    owner = await pool.fetchval(
        "SELECT tenant_id FROM catalog_items WHERE id = $1", item_id
    )
    if owner is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if owner != tenant_id:
        logger.warning(
            f"Cross-tenant catalog access attempt: "
            f"item {item_id} belongs to tenant {owner}, request from tenant {tenant_id}"
        )
        raise HTTPException(status_code=403, detail="Not authorized")

    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()
               if k in _CATALOG_UPDATE_FIELDS}
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    # Validate price range if both provided in this update
    new_min = updates.get("price_min_paise")
    new_max = updates.get("price_max_paise")
    if new_min is not None and new_max is not None and new_max < new_min:
        raise HTTPException(status_code=422, detail="price_max_paise must be >= price_min_paise")

    set_clauses = [f"{key} = ${i}" for i, key in enumerate(updates.keys(), start=1)]
    values = list(updates.values())
    values.append(item_id)

    await pool.execute(
        f"UPDATE catalog_items SET {', '.join(set_clauses)} WHERE id = ${len(values)}",
        *values,
    )
    logger.info(f"Catalog item updated: id={item_id} tenant={tenant_id} fields={list(updates.keys())}")
    return {"updated": True, "id": item_id}


@app.delete("/admin/catalog/{item_id}",
            dependencies=[Depends(require_internal_token)])
@limiter.limit("30/minute")
async def delete_catalog_item(request: Request, item_id: int, tenant_id: int):
    pool = await get_pg_pool()
    owner = await pool.fetchval(
        "SELECT tenant_id FROM catalog_items WHERE id = $1", item_id
    )
    if owner is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if owner != tenant_id:
        logger.warning(
            f"Cross-tenant delete attempt: item {item_id} owned by {owner}, "
            f"request from tenant {tenant_id}"
        )
        raise HTTPException(status_code=403, detail="Not authorized")

    await pool.execute("DELETE FROM catalog_items WHERE id = $1", item_id)
    logger.info(f"Catalog item deleted: id={item_id} tenant={tenant_id}")
    return {"deleted": True, "id": item_id}


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
