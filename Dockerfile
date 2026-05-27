# syntax=docker/dockerfile:1.7

# ──────────────────────────────────────────────────────────────────────────
# Multi-stage build for the voice agent.
# Stage 1 (builder): install build-time deps and Python packages into a venv.
# Stage 2 (runtime): copy only the venv + app code into a minimal image.
# This keeps the final image small and reduces attack surface (no compilers,
# no build tools in the running container).
# ──────────────────────────────────────────────────────────────────────────

# ─── Stage 1: Builder ────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS builder

# Build-time deps. Pipecat pulls in some packages with C extensions
# (silero VAD, audio processing).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a venv inside /opt so it's easy to copy to the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Install dependencies with hash verification (from Pass 5 lock file).
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# ─── Stage 2: Runtime ────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS runtime

# Runtime-only deps. No compilers in the final image.
# libsndfile1: audio I/O (pipecat)
# libxcb1, libgl1, libglib2.0-0: opencv-python (cv2) is a transitive pipecat
#   dep and loads X11/GL/GLib shared libs at import time even in headless use.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    libxcb1 \
    libgl1 \
    libglib2.0-0 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user. Running as root in containers is a security smell:
# if anything escapes the container, root inside = root outside (on
# misconfigured hosts).
# Note: libsndfile1 creates a system group named 'voice' (GID 22, Linux audio
# device group), so we use 'appuser' to avoid a name collision with useradd.
RUN groupadd -g 1000 appuser \
    && useradd --no-log-init -m -u 1000 -g 1000 -s /bin/bash appuser

# Copy the prebuilt venv from the builder stage.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy app code. The .dockerignore keeps .env, .venv, pgdata, and other
# junk out of the image.
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser scripts/ ./scripts/

# Drop to non-root.
USER appuser

# The default command runs the agent. The tools service overrides this
# in compose.yaml.
CMD ["python", "-m", "app.main"]
