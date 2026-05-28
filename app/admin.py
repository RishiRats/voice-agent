"""Admin dashboard — view/manage appointments and call logs.

Protected by HTTP Basic Auth (username: admin, password: ADMIN_TOKEN from .env).
Access at: https://<your-domain>/admin

Mount this router on pipecat's FastAPI app before calling main():
    from pipecat.runner.run import app as _pipecat_app
    from app.admin import router as _admin_router
    _pipecat_app.include_router(_admin_router)
"""
import base64
import secrets
from datetime import date, datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config

router = APIRouter()

_IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

def _check_auth(request: Request) -> bool:
    if not config.ADMIN_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        _, _, password = decoded.partition(":")
        return secrets.compare_digest(password, config.ADMIN_TOKEN)
    except Exception:
        return False


def _unauth() -> Response:
    return Response(
        "Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Voice Agent Admin"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# DB pool (separate from the agent pool so startup/shutdown are independent)
# ─────────────────────────────────────────────────────────────────────────────

_pg: asyncpg.Pool | None = None


async def _pool() -> asyncpg.Pool:
    global _pg
    if _pg is None:
        _pg = await asyncpg.create_pool(config.POSTGRES_URL, min_size=1, max_size=3)
    return _pg


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(_IST).strftime("%-d %b %Y, %I:%M %p")


def _status_badge(status: str) -> str:
    cls = {
        "booked":    "bg-blue-100 text-blue-700",
        "cancelled": "bg-red-100 text-red-700",
        "completed": "bg-green-100 text-green-700",
        "no_show":   "bg-yellow-100 text-yellow-700",
    }.get(status, "bg-gray-100 text-gray-700")
    return (
        f'<span class="inline-block px-2 py-0.5 rounded text-xs font-medium {cls}">'
        f"{status}</span>"
    )


def _outcome_badge(outcome: str | None) -> str:
    outcome = outcome or "unknown"
    cls = {
        "appointment_booked": "bg-green-100 text-green-700",
        "lead_captured":      "bg-blue-100 text-blue-700",
        "abandoned":          "bg-yellow-100 text-yellow-700",
    }.get(outcome, "bg-gray-100 text-gray-700")
    label = outcome.replace("_", " ").title()
    return (
        f'<span class="inline-block px-2 py-0.5 rounded text-xs font-medium {cls}">'
        f"{label}</span>"
    )


def _e(text: str | None) -> str:
    """HTML-escape a string."""
    if text is None:
        return "—"
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────────────

def _render_appointments(rows: list) -> str:
    if not rows:
        return '<tr><td colspan="6" class="py-6 text-center text-gray-400 text-sm">No appointments found.</td></tr>'
    out = []
    for r in rows:
        cancel_btn = ""
        if r["status"] == "booked":
            cancel_btn = (
                f'<form method="post" action="/admin/appointments/{r["id"]}/cancel">'
                f'<button type="submit" onclick="return confirm(\'Cancel this slot?\')"'
                f' class="text-xs font-medium text-red-600 hover:text-red-800">Cancel</button>'
                f"</form>"
            )
        out.append(
            f'<tr class="hover:bg-gray-50">'
            f'<td class="py-2 pr-4 font-medium text-gray-800 whitespace-nowrap">{_fmt_dt(r["slot_at"])}</td>'
            f'<td class="py-2 pr-4 text-gray-700">{_e(r["caller_name"])}</td>'
            f'<td class="py-2 pr-4 text-gray-600 whitespace-nowrap">{_e(r["caller_phone"])}</td>'
            f'<td class="py-2 pr-4 text-gray-500">{_e(r["notes"])}</td>'
            f'<td class="py-2 pr-4">{_status_badge(r["status"])}</td>'
            f'<td class="py-2">{cancel_btn}</td>'
            f"</tr>"
        )
    return "\n".join(out)


def _render_logs(rows: list) -> str:
    if not rows:
        return '<tr><td colspan="5" class="py-6 text-center text-gray-400 text-sm">No call logs yet.</td></tr>'
    out = []
    for r in rows:
        dur = f"{r['duration_secs']}s" if r["duration_secs"] else "—"
        caller = _e(r["caller_number"]) if r["caller_number"] else "Browser mic"
        summary = _e(r["summary"])
        out.append(
            f'<tr class="hover:bg-gray-50">'
            f'<td class="py-2 pr-4 text-gray-800 whitespace-nowrap">{_fmt_dt(r["started_at"])}</td>'
            f'<td class="py-2 pr-4 text-gray-700 whitespace-nowrap">{caller}</td>'
            f'<td class="py-2 pr-4 text-gray-500">{dur}</td>'
            f'<td class="py-2 pr-4">{_outcome_badge(r["outcome"])}</td>'
            f'<td class="py-2 text-gray-500">{summary}</td>'
            f"</tr>"
        )
    return "\n".join(out)


def _render_page(
    appt_rows: str,
    log_rows: str,
    tomorrow: str,
    msg: str = "",
) -> str:
    msg_html = (
        f'<div class="mb-4 px-4 py-2 bg-green-100 text-green-800 rounded text-sm">{_e(msg)}</div>'
        if msg else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Admin — Voice Agent</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen font-sans">
<div class="max-w-7xl mx-auto px-4 py-8">

  <div class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-2xl font-bold text-gray-900">Voice Agent Admin</h1>
      <p class="text-sm text-gray-500 mt-0.5">Sharma Dental Clinic &mdash; tenant #1 &mdash; times in IST</p>
    </div>
  </div>

  {msg_html}

  <!-- ── Appointments ─────────────────────────────────────────── -->
  <div class="bg-white rounded-xl shadow mb-6">
    <div class="flex items-center justify-between px-6 pt-6 pb-4 border-b border-gray-100">
      <h2 class="text-base font-semibold text-gray-800">Appointments</h2>
      <button onclick="document.getElementById('add-form').classList.toggle('hidden')"
              class="text-sm bg-blue-600 hover:bg-blue-700 text-white px-3 py-1.5 rounded-lg font-medium">
        + Add slot
      </button>
    </div>

    <!-- add form -->
    <div id="add-form" class="hidden px-6 py-4 bg-blue-50 border-b border-blue-100">
      <p class="text-xs font-semibold text-blue-600 uppercase tracking-wide mb-3">Manual entry</p>
      <form method="post" action="/admin/appointments"
            class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 items-end">
        <div>
          <label class="block text-xs text-gray-600 mb-1">Date</label>
          <input type="date" name="slot_date" required value="{tomorrow}"
                 class="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400">
        </div>
        <div>
          <label class="block text-xs text-gray-600 mb-1">Time</label>
          <input type="time" name="slot_time" required value="10:00"
                 class="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400">
        </div>
        <div>
          <label class="block text-xs text-gray-600 mb-1">Name</label>
          <input type="text" name="caller_name" placeholder="Patient name"
                 class="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400">
        </div>
        <div>
          <label class="block text-xs text-gray-600 mb-1">Phone</label>
          <input type="text" name="caller_phone" placeholder="+91XXXXXXXXXX"
                 class="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400">
        </div>
        <div>
          <label class="block text-xs text-gray-600 mb-1">Notes</label>
          <input type="text" name="notes" placeholder="Reason (optional)"
                 class="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400">
        </div>
        <div>
          <button type="submit"
                  class="w-full bg-green-600 hover:bg-green-700 text-white rounded px-3 py-1.5 text-sm font-medium">
            Book
          </button>
        </div>
      </form>
    </div>

    <div class="px-6 py-4 overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-xs font-medium text-gray-500 border-b border-gray-200">
            <th class="pb-2 pr-6">Date &amp; Time (IST)</th>
            <th class="pb-2 pr-6">Name</th>
            <th class="pb-2 pr-6">Phone</th>
            <th class="pb-2 pr-6">Notes</th>
            <th class="pb-2 pr-6">Status</th>
            <th class="pb-2">Action</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-50">
          {appt_rows}
        </tbody>
      </table>
    </div>
  </div>

  <!-- ── Call Logs ─────────────────────────────────────────────── -->
  <div class="bg-white rounded-xl shadow">
    <div class="px-6 pt-6 pb-4 border-b border-gray-100">
      <h2 class="text-base font-semibold text-gray-800">Recent Call Logs</h2>
    </div>
    <div class="px-6 py-4 overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-xs font-medium text-gray-500 border-b border-gray-200">
            <th class="pb-2 pr-6">Time (IST)</th>
            <th class="pb-2 pr-6">Caller</th>
            <th class="pb-2 pr-6">Duration</th>
            <th class="pb-2 pr-6">Outcome</th>
            <th class="pb-2">Summary</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-50">
          {log_rows}
        </tbody>
      </table>
    </div>
  </div>

</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, msg: str = ""):
    if not _check_auth(request):
        return _unauth()

    pool = await _pool()
    appts = await pool.fetch(
        """
        SELECT id, caller_name, caller_phone, slot_at, notes, status
        FROM appointments
        WHERE tenant_id = $1
          AND slot_at >= now() - interval '7 days'
        ORDER BY
          CASE WHEN status = 'booked' THEN 0 ELSE 1 END,
          slot_at ASC
        LIMIT 60
        """,
        config.DEMO_TENANT_ID,
    )
    logs = await pool.fetch(
        """
        SELECT call_id, caller_number, started_at, duration_secs, outcome, summary
        FROM call_logs
        WHERE tenant_id = $1
        ORDER BY started_at DESC
        LIMIT 30
        """,
        config.DEMO_TENANT_ID,
    )

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    html = _render_page(
        appt_rows=_render_appointments(list(appts)),
        log_rows=_render_logs(list(logs)),
        tomorrow=tomorrow,
        msg=msg,
    )
    return HTMLResponse(html)


@router.post("/admin/appointments/{appt_id}/cancel")
async def cancel_appointment(appt_id: int, request: Request):
    if not _check_auth(request):
        return _unauth()
    pool = await _pool()
    await pool.execute(
        "UPDATE appointments SET status = 'cancelled' WHERE id = $1 AND tenant_id = $2",
        appt_id,
        config.DEMO_TENANT_ID,
    )
    return RedirectResponse("/admin?msg=Appointment+cancelled", status_code=303)


@router.post("/admin/appointments")
async def add_appointment(
    request: Request,
    slot_date: str = Form(...),
    slot_time: str = Form(...),
    caller_name: str = Form(""),
    caller_phone: str = Form(""),
    notes: str = Form(""),
):
    if not _check_auth(request):
        return _unauth()

    slot_dt = datetime.fromisoformat(f"{slot_date}T{slot_time}:00")
    pool = await _pool()
    try:
        await pool.execute(
            """
            INSERT INTO appointments
              (tenant_id, caller_name, caller_phone, slot_at, notes, status)
            VALUES ($1, $2, $3, $4, $5, 'booked')
            """,
            config.DEMO_TENANT_ID,
            caller_name.strip() or None,
            caller_phone.strip() or None,
            slot_dt,
            notes.strip() or None,
        )
    except Exception:
        return RedirectResponse("/admin?msg=Slot+already+taken", status_code=303)

    return RedirectResponse("/admin?msg=Appointment+added", status_code=303)
