# Security Audit — Closure Report

Pre-deployment security audit conducted 2026-05-27. 25 findings across 5 severities.

## Closed before deploy (Passes 1–5)

| # | Severity | Category | Pass |
|---|----------|----------|------|
| 1 | CRITICAL | Tools API auth (X-Internal-Token shared secret) | 4a |
| 2 | CRITICAL | WebRTC endpoint auth | 4b (partial — full closure in Stage 3) |
| 3 | CRITICAL | Concurrency cap (asyncio.Semaphore) | 3 |
| 4 | CRITICAL | tenant_id overwrite in tool handlers | 1 |
| 5 | CRITICAL | Double-booking race condition | 1 |
| 6 | HIGH | Tools server 0.0.0.0 bind → localhost-only | 2 |
| 7 | HIGH | Postgres/Redis exposed + weak credentials | 2 |
| 8 | HIGH | No max call duration → cost exposure | 3 |
| 10 | HIGH | Summary prompt injection | 5 |
| 11 | HIGH | Tools API rate limiting (slowapi) | 3 |
| 12, 13 | MEDIUM | Full PII (phone + name) in logs | 5 |
| 14 | MEDIUM | No data retention policy | 5 |
| 15 | MEDIUM | sarvamkb.txt committed to git | 5 |
| 16 | MEDIUM | Dead WEBRTC_HOST config variable | 2 |
| 19 | MEDIUM | Unpinned dependencies (supply-chain risk) | 5 |
| 21 | LOW | Stale model name in .env.example | 1 |
| 25 | LOW | google-genai pulled transitively, not declared | 5 |

## Deferred to Stage 3 (forward-looking findings)

- **#2 final closure**: Exotel WebSocket origin validation. `DISABLE_TEST_CLIENT=true` currently exits at startup. The Exotel WSS transport wired in Stage 3 will close this fully.

## Deferred to Stage 4+ (production-scaling findings)

| # | Finding | Reason deferred |
|---|---------|----------------|
| 9 | Bash history wipe | Laptop hygiene; user-decided |
| 17 | Past-time slot filtering | UX improvement, not a security issue |
| 18 | Cross-tenant slot leakage | Moot once #1 (auth) was closed |
| 20 | Dockerfile hardening | Handled as part of deploy work, not security pass |
| 22 | FastAPI 422 detail leakage | Acceptable for internal-only service |
| 23 | Greeting log content | Acceptable while greetings are static text |
| 24 | Redis password rotation | Covered by #7 |

## Verified attack scenarios

The following attacks were tested and confirmed unsuccessful after the patches:

1. **Direct curl to tools API without token** → 401 Missing X-Internal-Token
2. **Curl with wrong token** → 401 Invalid token + warning logged
3. **tenant_id injection via tool arguments** → server overwrites with authenticated tenant
4. **Concurrent slot booking (race)** → exactly one wins via `SELECT FOR UPDATE` + partial unique index
5. **>MAX_CONCURRENT_CALLS connections** → excess connections rejected immediately
6. **Call exceeding MAX_CALL_DURATION_SECS** → forced TTS goodbye + EndFrame termination
7. **>60 tool requests/minute** → 429 Too Many Requests
8. **Postgres/Redis connection from non-localhost IP** → Connection refused
9. **Production-mode startup (DISABLE_TEST_CLIENT=true)** → exits at startup with clear error
10. **Prompt injection in summary** → injected instruction inside `<transcript>` tags, treated as DATA

## How to install from the locked requirements

```bash
# Production install — verifies SHA256 hashes of every package
pip install --require-hashes -r requirements.txt

# To add a new dependency (dev workflow)
echo "newpackage>=1.0" >> requirements.in
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
pip install --require-hashes -r requirements.txt
```
