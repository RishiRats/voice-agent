# Voice Agent — Stage 1

Multi-tenant Indic voice agent built on Pipecat + Sarvam + FastAPI.

**Stage 1 goal:** A working voice loop you can talk to in your browser. No telephony yet.
You will:
1. Open `http://localhost:7860` in your browser
2. Click "Start" — your mic is captured
3. Speak in Hindi / English / code-mix
4. Hear a response from a fake dental clinic AI receptionist named Priya

Total time to first conversation: ~20 minutes after you have your Sarvam API key.

---

## Architecture (Stage 1)

```
Browser (mic + speakers)
   │ WebRTC
   ▼
SmallWebRTCTransport (Pipecat built-in)
   │
   ▼
Pipecat pipeline:
   SarvamSTTService (Saaras v3)  →  SarvamLLMService (Sarvam-30B)  →  SarvamTTSService (Bulbul v3)
                                            │
                                            ├── system prompt loaded from Postgres tenants table
                                            ├── conversation history in Redis (TTL 1h)
                                            └── tool calls → FastAPI /tools/*
```

In Stage 2 we'll add the FastAPI tools service.
In Stage 3 we'll add Asterisk + Exotel for actual phone calls.

---

## Prerequisites (Fedora)

```bash
sudo dnf install -y python3.12 python3.12-devel gcc git podman podman-compose
# pipx for managing tools
sudo dnf install -y pipx
pipx ensurepath
# uv (fast Python package manager, recommended by Pipecat)
pipx install uv
```

You can use `docker` + `docker-compose` instead of podman if you prefer — Fedora ships podman by default but everything in `compose.yaml` works with both.

---

## Setup

```bash
# 1. Clone and enter
git clone <this-repo> voice-agent
cd voice-agent

# 2. Create Python env
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt

# 3. Copy env template, fill in your Sarvam key
cp .env.example .env
$EDITOR .env   # paste your SARVAM_API_KEY

# 4. Start Postgres + Redis
podman-compose up -d
# Or: docker-compose up -d

# 5. Run migrations (creates tenants + call_logs tables, seeds demo tenant)
python -m app.db.migrate

# 6. Start the voice agent
python -m app.main

# 7. Open browser
xdg-open http://localhost:7860
```

---

## What's in here

```
voice-agent/
├── app/
│   ├── main.py                 # Pipecat pipeline entrypoint
│   ├── config.py               # Env vars + tenant loading
│   ├── services/
│   │   └── tenant_loader.py    # Loads system_prompt for the active call's tenant
│   ├── db/
│   │   ├── schema.sql          # Postgres schema
│   │   ├── seed.sql            # Demo tenant: "Sharma Dental Clinic"
│   │   └── migrate.py          # Runs schema + seed
│   └── tools/                  # Stage 2: FastAPI tools service goes here
├── compose.yaml                # Postgres + Redis
├── requirements.txt
└── .env.example
```

---

## Multi-tenancy model

Each tenant = one business. In Stage 1, we identify the active tenant via an env var
(`DEMO_TENANT_ID=1` — the seeded Sharma Dental Clinic). In Stage 3, the tenant is looked up
by the dialed-in DID phone number (`SELECT id FROM tenants WHERE inbound_did = $1`).

The `tenants` table holds everything that makes one agent different from another:
- `system_prompt` — the entire personality + business info
- `greeting` — first line spoken when caller connects
- `voice` — which Bulbul voice (anushka, meera, etc.)
- `default_language` — hi-IN, ta-IN, en-IN, etc.
- `llm_model` — sarvam-30b or sarvam-105b
- `inbound_did` — the phone number this tenant owns (used in Stage 3)
- `tools_enabled` — JSON list of which tool endpoints this tenant can call

Adding a new tenant later is just `INSERT INTO tenants ...`. No code change.
