#!/usr/bin/env bash
set -euo pipefail
# Load .env so WEBRTC_HOST and WEBRTC_PORT are available.
# Pipecat's runner reads --host / --port from CLI args; it does NOT read env vars directly.
set -a
# shellcheck disable=SC1091
source "$(dirname "$0")/../.env"
set +a
exec python -m app.main --host "${WEBRTC_HOST:-127.0.0.1}" --port "${WEBRTC_PORT:-7860}"
