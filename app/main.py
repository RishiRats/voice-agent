"""Voice agent entrypoint — Stage 1.

Runs the Pipecat pipeline locally. Connects to your browser via WebRTC:
    Browser mic ──► Sarvam Saaras (STT) ──► Sarvam-30B (LLM) ──► Sarvam Bulbul (TTS) ──► Browser speakers

To run:
    python -m app.main

Then open http://localhost:7860/client in your browser, click Connect, and talk.

ARCHITECTURE NOTES
==================
Pipecat is a "pipeline of frame processors". Audio comes in as raw frames from
the transport (WebRTC in our case), passes through STT → LLM → TTS, and
goes back out as audio frames. Each processor is async and can run concurrently
with the others.

Sarvam's STT WebSocket has server-side VAD built in — it segments speech
automatically, so we don't need a local SileroVADAnalyzer in the pipeline.

The LLMContext object holds the conversation history. Pipecat's context aggregator
automatically appends each user turn (post-STT) and each assistant turn (pre-TTS).
That gives us per-call short-term memory for free. Redis persistence comes in Stage 2.

Multi-tenancy: when a client connects, we load the tenant's system_prompt from
Postgres and build the LLMContext with it. Stage 1 uses DEMO_TENANT_ID. Stage 3
will read the dialed DID from Asterisk SIP headers.
"""
import asyncio
import sys

import asyncpg
from dotenv import load_dotenv
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import TransportParams

from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService

from app import config
from app.services.tenant_loader import load_tenant_by_id, Tenant

load_dotenv()

# Configure loguru
logger.remove()
logger.add(sys.stderr, level=config.LOG_LEVEL,
           format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<8}</level> | {message}")


# ============================================================================
# Postgres connection pool — shared across all in-flight calls.
# ============================================================================

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


# ============================================================================
# Build the per-call pipeline.
# ============================================================================

async def build_pipeline_for_call(transport, tenant: Tenant) -> tuple[PipelineTask, LLMContext]:
    """Construct a Pipecat pipeline customized for this tenant."""

    logger.info(f"Building pipeline for tenant {tenant.id} ({tenant.name!r})")
    logger.info(f"  Language: {tenant.default_language}  Voice: {tenant.voice}  LLM: {tenant.llm_model}")

    # ----- STT: Sarvam Saaras v3, streaming WebSocket -----
    # mode='transcribe' keeps output in the source language (Hindi stays Hindi).
    # The WebSocket connection has server-side VAD built in — no local VAD needed.
    # Pass language as the raw BCP-47 string; the Language enum serializes to
    # a short code ('hi') that the Sarvam API doesn't accept.
    stt = SarvamSTTService(
        api_key=config.SARVAM_API_KEY,
        mode="transcribe",
        sample_rate=16000,
        settings=SarvamSTTService.Settings(
            model="saaras:v3",
            language=tenant.default_language,  # e.g. "hi-IN" — must be string, not Language enum
        ),
    )

    # ----- LLM: priority order — Gemini > Ollama > Sarvam -----
    # Set GEMINI_MODEL + GOOGLE_API_KEY to use Gemini Flash (best quality, free tier).
    # Set OLLAMA_MODEL to use a local Ollama model (offline fallback).
    # Leave both unset to use Sarvam-30B (production default).
    if config.GEMINI_MODEL and config.GOOGLE_API_KEY:
        logger.info(f"  Using Gemini LLM: {config.GEMINI_MODEL}")
        llm = GoogleLLMService(
            api_key=config.GOOGLE_API_KEY,
            settings=GoogleLLMService.Settings(
                model=config.GEMINI_MODEL,
                temperature=tenant.temperature,
                max_tokens=512,
            ),
        )
    elif config.OLLAMA_MODEL:
        logger.info(f"  Using Ollama LLM: {config.OLLAMA_MODEL} at {config.OLLAMA_BASE_URL}")
        llm = OpenAILLMService(
            api_key="ollama",
            base_url=config.OLLAMA_BASE_URL,
            settings=OpenAILLMService.Settings(
                model=config.OLLAMA_MODEL,
                temperature=tenant.temperature,
                max_tokens=512,
            ),
        )
    else:
        logger.info(f"  Using Sarvam LLM: {tenant.llm_model}")
        llm = SarvamLLMService(
            api_key=config.SARVAM_API_KEY,
            settings=SarvamLLMService.Settings(
                model=tenant.llm_model,
                temperature=tenant.temperature,
                max_tokens=512,
            ),
        )

    # ----- TTS: Sarvam Bulbul v3, WebSocket streaming -----
    # voice must be a bulbul:v3 speaker name (lowercase). bulbul:v2 names like
    # 'anushka' are NOT valid for v3 and will silently use the model default.
    # See seed.sql for the configured voice; default is 'priya'.
    # sample_rate=16000: standard voice call quality; matches STT input rate.
    # Stage 3 (Asterisk/PSTN): switch to sample_rate=8000 + mulaw codec.
    tts = SarvamTTSService(
        api_key=config.SARVAM_API_KEY,
        sample_rate=16000,
        settings=SarvamTTSService.Settings(
            model="bulbul:v3",
            voice=tenant.voice,
            language=tenant.default_language,  # string, not Language enum
            temperature=0.6,  # balanced: natural yet reliable (per Sarvam docs)
            pace=1.0,         # natural speed for conversational agent
        ),
    )

    # ----- Conversation context -----
    initial_messages = [
        {"role": "system", "content": tenant.system_prompt},
    ]
    context = LLMContext(initial_messages)
    # vad_analyzer enables fast barge-in: VADUserTurnStartStrategy fires after
    # start_secs=0.1 (100ms) of detected speech, interrupting Priya immediately.
    # Without this, the default falls through to TranscriptionUserTurnStartStrategy
    # which only fires when Sarvam STT returns text (end-of-speech → too late).
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    start_secs=0.1,   # interrupt after 100ms of detected speech
                    stop_secs=0.4,    # confirm speech end after 400ms silence
                    confidence=0.6,   # moderate threshold — not too sensitive to noise
                    min_volume=0.4,   # ignore low-level background noise
                )
            )
        ),
    )

    # ----- Pipeline -----
    # Frame flow: mic audio → STT (with server-side VAD) → user turn appended →
    # LLM generates → TTS speaks → audio out → assistant turn appended.
    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    return task, context


# ============================================================================
# Per-call entry point — called by Pipecat's runner when a client connects.
# ============================================================================

async def bot(runner_args: RunnerArguments):
    """Entrypoint: called once per WebRTC connection (or per phone call in Stage 3)."""

    pool = await get_pg_pool()
    tenant = await load_tenant_by_id(pool, config.DEMO_TENANT_ID)

    transport = await create_transport(
        runner_args,
        transport_params={
            "webrtc": lambda: TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
            )
        },
    )

    task, context = await build_pipeline_for_call(transport, tenant)

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        logger.info(f"Client connected. Greeting: {tenant.greeting!r}")
        context.messages.append({"role": "assistant", "content": tenant.greeting})
        await task.queue_frame(TTSSpeakFrame(tenant.greeting))

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        logger.info("Client disconnected.")
        for msg in context.messages:
            logger.debug(f"  [{msg.get('role')}] {msg.get('content')!r}")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


# ============================================================================
# Module entry point — `python -m app.main`
# ============================================================================

if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
