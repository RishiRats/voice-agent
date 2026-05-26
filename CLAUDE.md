# CLAUDE.md — Project Context for Claude Code

This document captures the full context of this project so Claude Code can pick up immediately. Read this first before touching any code.

---

## What we're building

A **multi-tenant Indic voice AI agent** for Indian SMBs. The product helps Indian businesses (dental clinics, real estate offices, restaurants, salons, etc.) recover revenue lost to missed phone calls. Indian SMBs lose ₹2–5 lakh/month to unanswered calls, and 85% of callers who don't reach a human go to a competitor instead.

**End state:** A caller dials an Indian phone number → an AI receptionist (in Hindi, English, or code-mixed Hinglish) answers within 1 ring, qualifies them, books appointments, transfers complex cases to humans, and logs everything to the business's CRM.

## Architectural decisions already made

| Decision | Choice | Reason |
|---|---|---|
| Voice framework | **Pipecat** (BSD 2-Clause) | Native Sarvam adapters since v0.0.108, real-time pipeline, commercially clean license |
| STT | **Sarvam Saaras v3** | Best-in-class for Indian languages, 8kHz telephony-optimised, multiple output modes |
| LLM | **Sarvam-30B** (free tier currently, Apache 2.0) | Native tool calling, low latency, multilingual, can self-host the weights later |
| TTS | **Sarvam Bulbul v3** | Natural Indian voices, WebSocket streaming for low latency |
| Telephony bridge | **Asterisk** (GPL) | Battle-tested, huge community, fine to USE without GPL-tainting our code |
| PSTN trunk | **Exotel** (commercial service) | Indian provider, SIP support, regulatory-compliant for inbound Indian numbers |
| Orchestration | **FastAPI** (MIT) | Replaces n8n. n8n's Sustainable Use License prohibits embedding in a SaaS product, which is what we're building |
| Session memory | **Redis** + Pipecat's in-memory LLMContext | Redis for crash recovery, in-memory for the hot path |
| Long-term storage | **PostgreSQL** | Tenant config, call logs, transcripts, appointments |
| Dev environment | **Fedora Linux**, podman, Python 3.12, uv | User's setup |

## Decisions explicitly rejected

- **n8n** — Sustainable Use License prohibits SaaS embedding. We're building SaaS, so n8n is out for the orchestration layer (but is fine for prototyping or for internal-only deployments).
- **LiveKit/Vapi/Retell** as managed services — too expensive at scale and we want to own the stack.
- **Twilio for India inbound** — TRAI regulations make Twilio impractical for Indian inbound DIDs.
- **Sarvam-M** — now legacy, replaced by Sarvam-30B/105B.
- **Saarika v2.5** — being deprecated, use Saaras v3 with mode="transcribe".

## Build plan (staged)

- **Stage 1 (current):** Voice loop working via browser mic. STT + LLM + TTS in one pipeline. Multi-tenant config in Postgres. No telephony, no tools yet. Goal: hear a working Priya AI receptionist in your browser.
- **Stage 2:** FastAPI tools service (`check_availability`, `book_appointment`, `handoff_to_human`). Redis-backed conversation persistence. Post-call transcript writes to Postgres.
- **Stage 3:** Asterisk + Exotel SIP trunk. Tenant lookup by dialed DID instead of env var. Real phone calls.
- **Stage 4:** Admin UI to manage tenants (system prompts, voices, business hours). Per-call cost tracking and billing.
- **Stage 5:** Production hardening — observability, multi-region deploy, failover.

## Current state — what's already scaffolded

Stage 1 scaffolding exists in this repo. Files:

```
voice-agent/
├── CLAUDE.md             ← this file
├── README.md             ← user-facing setup guide
├── compose.yaml          ← Postgres + Redis (podman/docker)
├── requirements.txt      ← pipecat-ai[sarvam,webrtc,silero], asyncpg, redis, python-dotenv
├── .env.example          ← env var template
└── app/
    ├── __init__.py
    ├── config.py         ← loads env vars, fails fast if anything is missing
    ├── main.py           ← Pipecat pipeline entrypoint (SmallWebRTC transport)
    ├── smoketest.py      ← verifies DB + tenant load without needing Sarvam key
    ├── db/
    │   ├── __init__.py
    │   ├── schema.sql    ← tenants, call_logs, appointments tables
    │   ├── seed.sql      ← demo tenant: Sharma Dental Clinic in Andheri
    │   └── migrate.py    ← runs schema + seed (idempotent)
    └── services/
        ├── __init__.py
        └── tenant_loader.py  ← typed Tenant dataclass + load_tenant_by_id/did
```

## Critical thing to know about the scaffolding

**I (the Claude that wrote this scaffolding) did not run it.** Pipecat's API has moved fast — the SarvamLLMService landed in v0.0.108, the universal LLMContext replaced OpenAILLMContext in v0.0.99, and the runner/transport setup has changed multiple times. The code is written against what the latest docs and release notes describe, but it has NOT been executed.

**Your first job in Claude Code is to actually run it and fix whatever's broken.** Likely failure points:

1. The `create_transport` call in `app/main.py` uses an `__import__` trick to avoid a module-load-time import. If Pipecat's transport import paths have changed, fix this with a normal `from pipecat.transports.smallwebrtc.transport import TransportParams` at the top.
2. The `SarvamLLMService.InputParams` class might be `SarvamLLMService.Settings` in the version that actually installs — check `pipecat.services.sarvam.llm` after install.
3. The `LLMContext` import path. If it's not at `pipecat.processors.aggregators.llm_context`, find it with `python -c "import pipecat; help(pipecat)"` or grep the installed package.
4. The Pipecat `runner.run.main` entrypoint signature — there's a `run_bot(runner_args)` convention but the exact import path moves around.

The right debugging approach: install Pipecat first, then run `python -m app.smoketest` (which doesn't import Pipecat at all and just tests Postgres + tenant load). If that works, the infrastructure is good. Then run `python -m app.main` and fix Pipecat-version-specific imports as they fail.

## Key references when fixing things

- Pipecat docs: https://docs.pipecat.ai
- Pipecat Sarvam STT: https://docs.pipecat.ai/server/services/stt/sarvam (may need to check, this URL was inferred)
- Pipecat Sarvam TTS: https://docs.pipecat.ai/server/services/tts/sarvam
- Pipecat releases (check what landed when): https://github.com/pipecat-ai/pipecat/releases
- Sarvam Saaras v3 API: https://docs.sarvam.ai/api-reference-docs/getting-started/models/saaras
- Sarvam-30B API: https://docs.sarvam.ai/api-reference-docs/getting-started/models/sarvam-30b
- Sarvam Bulbul v3 API: https://docs.sarvam.ai/api-reference-docs/api-guides-tutorials/text-to-speech/overview
- Sarvam pricing (LLMs are currently free!): https://www.sarvam.ai/api-pricing
- The full conversation that led here: ask the user to summarize, or look at git history if they've imported the previous Claude's notes

## User context (Rishi)

- Based in Dublin, Ireland
- Engineering background: edtech (Python tutoring), AR/VR at Planctech, infrastructure turnaround at BARC
- Job hunting in Dublin, exploring tech strategy/transformation consulting (EY) and cybersecurity (ThreatDown MDR)
- Building this project partly as a portfolio piece — emphasis on real engineering, not no-code automation
- Comfortable with Python, Linux, the terminal
- Prefers concise, direct technical communication. Doesn't want hand-holding on basics.

## How the previous Claude was working with Rishi

- Asked clarifying questions BEFORE writing code when architecture decisions were at stake
- Was honest about what wouldn't work (e.g., flat-out rejected n8n for real-time PSTN even though Rishi initially wanted it)
- Verified license terms before recommending stacks
- Searched the web for current Pipecat/Sarvam API patterns rather than relying on training-data memory
- Made the multi-tenant design explicit in the schema so onboarding new customers is "INSERT INTO tenants", not a code change

Continue in that mode. Rishi appreciates pushback when something is technically wrong.

## Immediate next actions (your TODO list when you start)

1. Read `README.md` and `app/db/schema.sql` to understand the data model.
2. Read `app/main.py` to understand the pipeline.
3. Run `podman-compose up -d` (or `docker-compose up -d`).
4. Run `python -m app.db.migrate` — confirm the demo tenant lands in Postgres.
5. Run `python -m app.smoketest` — confirm infrastructure is healthy.
6. Ask Rishi for his Sarvam API key (or wait if he doesn't have it yet).
7. Run `python -m app.main` — fix any Pipecat-version-specific import errors as they appear.
8. Open http://localhost:7860 and have a conversation with Priya the AI receptionist.
9. Iterate on the system prompt in `app/db/seed.sql` — that's where the agent's personality lives.

## When Stage 1 works, the user-visible Stage 2 starts

After Rishi can talk to Priya in the browser and have a coherent multi-turn conversation, Stage 2 is:

- Spin up a FastAPI service in `app/tools/` with `POST /tools/check_availability`, `POST /tools/book_appointment`, `POST /tools/handoff_to_human` endpoints
- Wire those into the Sarvam LLM as function-calling tools via Pipecat's tools schema
- Add Redis-backed conversation persistence so a worker crash mid-call doesn't lose state
- On `on_client_disconnected`, persist the full transcript to the `call_logs` table

Don't start Stage 2 until Stage 1 is solid. The temptation to add tools before the voice loop is rock-solid will lead to debugging hell.

## Production deployment toggle

`DISABLE_TEST_CLIENT=true` disables the browser-based test runner. This is
the production setting — public VPS deployments must use this.

Currently (Stage 2), `DISABLE_TEST_CLIENT=true` causes the process to exit
on startup because no production transport is wired up yet. The Exotel
WebSocket transport in Stage 3 will fill this in.

In dev, leave `DISABLE_TEST_CLIENT=false` and connect via
http://localhost:7860/client as before.
