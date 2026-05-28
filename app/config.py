"""Single source of truth for env vars. Fail loudly on startup if anything required is missing."""
import os
import sys

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        logger.error(f"Required env var {name} is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    return val


SARVAM_API_KEY = _require("SARVAM_API_KEY")
POSTGRES_URL = _require("POSTGRES_URL")
REDIS_URL = _require("REDIS_URL")
DEMO_TENANT_ID = int(os.environ.get("DEMO_TENANT_ID", "1"))
# WEBRTC_HOST / WEBRTC_PORT are not read here — Pipecat's runner accepts
# --host and --port CLI args. Use scripts/run-agent.sh which passes them from env.
WEBRTC_PORT = int(os.environ.get("WEBRTC_PORT", "7860"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Offline LLM fallback via Ollama. When OLLAMA_MODEL is set, the pipeline uses
# Ollama instead of SarvamLLMService. Useful when Sarvam API is down.
# Example: OLLAMA_MODEL=qwen2.5:7b
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

# Google Gemini LLM fallback. When GOOGLE_API_KEY is set AND GEMINI_MODEL is set,
# the pipeline uses GoogleLLMService. Takes priority over Ollama.
# Example: GEMINI_MODEL=gemini-3.5-flash
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")

# FastAPI tools service — binds to 127.0.0.1 by default so it's never reachable
# from outside the host. TOOLS_BASE_URL is derived automatically; only override
# it for unusual deployments (e.g., tools server on a different host).
TOOLS_HOST = os.environ.get("TOOLS_HOST", "127.0.0.1")
TOOLS_PORT = int(os.environ.get("TOOLS_PORT", "8000"))
TOOLS_BASE_URL = os.environ.get("TOOLS_BASE_URL", f"http://{TOOLS_HOST}:{TOOLS_PORT}")

# Cost protection caps.
MAX_CONCURRENT_CALLS = int(os.environ.get("MAX_CONCURRENT_CALLS", "3"))
MAX_CALL_DURATION_SECS = int(os.environ.get("MAX_CALL_DURATION_SECS", "600"))

# Shared secret between main.py (caller) and tools/server.py (callee).
# Required — process exits at startup if missing.
TOOLS_INTERNAL_TOKEN = _require("TOOLS_INTERNAL_TOKEN")

# Production flag. Set to 'true' to disable the browser /client test page.
DISABLE_TEST_CLIENT = os.environ.get("DISABLE_TEST_CLIENT", "false").lower() == "true"

# Admin dashboard password (HTTP Basic Auth, username: admin).
# If not set, /admin returns 401 with a clear error.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
