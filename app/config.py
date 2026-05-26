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
WEBRTC_HOST = os.environ.get("WEBRTC_HOST", "0.0.0.0")
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

# FastAPI tools service URL. Pipecat tool handlers call this to check availability
# and book appointments.
TOOLS_BASE_URL = os.environ.get("TOOLS_BASE_URL", "http://localhost:8000")
