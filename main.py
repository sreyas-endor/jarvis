"""Jarvis voice agent — WebRTC server. iPhone client connects, Mac runs pipeline.

Pipeline per call: SmallWebRTCTransport.input → VAD → STT → ClaudeCodeLLMService →
TTS → EventLogger → SmallWebRTCTransport.output. STT and TTS providers selected
via STT_PROVIDER and TTS_PROVIDER env vars; see README.

Mac stays running headless (lid closed, on AC). iOS app POSTs SDP offer to
/api/offer; we spin up a fresh pipeline bound to that connection, tear down
when the client disconnects.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Outbound IPv6 to api.cartesia.ai (CloudFront) silently times out on this
# network — curl works because it does Happy Eyeballs, but the `websockets`
# library picks the first getaddrinfo result and stalls 20s on the v6 socket.
# Filter v6 out of DNS resolution for this process so every outbound socket
# uses v4. Process-scoped only; nothing system-wide changes.
import socket as _socket

_real_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    return [r for r in _real_getaddrinfo(*args, **kwargs) if r[0] == _socket.AF_INET]


_socket.getaddrinfo = _ipv4_only_getaddrinfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.azure.tts import AzureTTSService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.whisper.stt import MLXModel, WhisperMLXSTTSettings
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from jarvis.azure_openai_tts_service import AzureOpenAITTSService
from jarvis.azure_phraselist_stt_service import AzurePhraseListSTTService
from jarvis.claude_code_llm_service import ClaudeCodeLLMService
from jarvis.whisper_jargon_stt_service import WhisperJargonSTTService

SKYLAR_VOICE_ID = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
AZURE_DEFAULT_VOICE = "en-US-AvaMultilingualNeural"
AZURE_DEFAULT_STYLE = "friendly"
AZURE_DEFAULT_STYLE_DEGREE = "1.5"
AZURE_DEFAULT_PITCH: str | None = None

OPENAI_DEFAULT_DEPLOYMENT = "gpt-4o-mini-tts"
OPENAI_DEFAULT_API_VERSION = "2024-10-01-preview"
OPENAI_DEFAULT_VOICE = "fable"
OPENAI_DEFAULT_SPEED = 1.0
OPENAI_DEFAULT_INSTRUCTIONS = (
    "Speak like a warm, engaged podcast host chatting with a close friend. "
    "Natural conversational pace, genuine smile in your voice."
)

MAI_DEFAULT_VOICE = "en-US-June:MAI-Voice-1"

JARGON_PHRASES = [
    "Claude", "Claude Code", "Anthropic", "Pipecat", "Cartesia",
    "Whisper", "MLX", "ElevenLabs", "OpenAI", "Cursor",
    "monorepo", "Endor Labs", "kubectl", "Bazel", "Helm",
    "Docker", "Kubernetes", "ArgoCD", "Terraform",
    "GitHub", "GitLab", "Slack", "Jira", "Atlassian", "Confluence",
    "Linear", "Notion",
    "Python", "TypeScript", "JavaScript", "Go",
    "Azure", "GCP", "AWS", "BigQuery", "MongoDB",
    "gRPC", "protobuf", "REST", "SQL",
    "LLM", "STT", "TTS", "MCP", "SDK", "CLI", "PR", "CI/CD",
    "npm", "yarn", "uv", "pip", "git", "ssh", "vim", "VSCode",
]

WHISPER_INITIAL_PROMPT = (
    "This is a casual conversation with a developer. Common terms include "
    + ", ".join(JARGON_PHRASES) + "."
)

WORKSPACE = Path(__file__).parent / "workspace"

# WebRTC server defaults. Bind 0.0.0.0 so the iPhone (over Tailscale or LAN)
# can reach us — Tailscale will expose the port on the tailnet IP.
JARVIS_HOST = os.environ.get("JARVIS_HOST", "0.0.0.0")
JARVIS_PORT = int(os.environ.get("JARVIS_PORT", "7860"))


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"{name} missing. Set it in ~/Code/jarvis/.env")
    return val


def _build_stt():
    provider = os.environ.get("STT_PROVIDER", "azure").lower()
    if provider == "azure":
        return AzurePhraseListSTTService(
            api_key=_require_env("AZURE_SPEECH_KEY"),
            region=_require_env("AZURE_SPEECH_REGION"),
            phrases=JARGON_PHRASES,
        )
    if provider == "whisper":
        return WhisperJargonSTTService(
            initial_prompt=WHISPER_INITIAL_PROMPT,
            settings=WhisperMLXSTTSettings(model=MLXModel.LARGE_V3_TURBO_Q4.value),
        )
    raise RuntimeError(
        f"Unknown STT_PROVIDER={provider!r}. Use 'azure' or 'whisper'."
    )


def _build_tts():
    provider = os.environ.get("TTS_PROVIDER", "cartesia").lower()
    if provider == "cartesia":
        return CartesiaTTSService(
            api_key=_require_env("CARTESIA_API_KEY"),
            settings=CartesiaTTSService.Settings(
                model="sonic-3",
                voice=SKYLAR_VOICE_ID,
                language=Language.EN,
            ),
        )
    if provider == "azure":
        return AzureTTSService(
            api_key=_require_env("AZURE_SPEECH_KEY"),
            region=_require_env("AZURE_SPEECH_REGION"),
            settings=AzureTTSService.Settings(
                voice=os.environ.get("AZURE_SPEECH_VOICE", AZURE_DEFAULT_VOICE),
                language="en-US",
                style=os.environ.get("AZURE_SPEECH_STYLE", AZURE_DEFAULT_STYLE),
                style_degree=os.environ.get(
                    "AZURE_SPEECH_STYLE_DEGREE", AZURE_DEFAULT_STYLE_DEGREE
                ),
                pitch=os.environ.get("AZURE_SPEECH_PITCH", AZURE_DEFAULT_PITCH),
                rate=os.environ.get("AZURE_SPEECH_RATE"),
            ),
        )
    if provider == "mai":
        return AzureTTSService(
            api_key=_require_env("AZURE_SPEECH_KEY"),
            region=_require_env("AZURE_SPEECH_REGION"),
            settings=AzureTTSService.Settings(
                voice=os.environ.get("MAI_VOICE", MAI_DEFAULT_VOICE),
                language="en-US",
            ),
        )
    if provider == "openai":
        return AzureOpenAITTSService(
            api_key=_require_env("AZURE_OPENAI_API_KEY"),
            endpoint=_require_env("AZURE_OPENAI_ENDPOINT"),
            api_version=os.environ.get(
                "AZURE_OPENAI_API_VERSION", OPENAI_DEFAULT_API_VERSION
            ),
            deployment=os.environ.get(
                "AZURE_OPENAI_TTS_DEPLOYMENT", OPENAI_DEFAULT_DEPLOYMENT
            ),
            voice=os.environ.get("OPENAI_TTS_VOICE", OPENAI_DEFAULT_VOICE),
            instructions=OPENAI_DEFAULT_INSTRUCTIONS,
            speed=OPENAI_DEFAULT_SPEED,
        )
    raise RuntimeError(
        f"Unknown TTS_PROVIDER={provider!r}. Use 'cartesia', 'azure', 'openai', or 'mai'."
    )


class EventLogger(FrameProcessor):
    """Prints every meaningful conversation event so the pipeline is readable."""

    def __init__(self) -> None:
        super().__init__()
        self._tts_audio_bytes = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            print(">>> speaking")
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            print("<<< stopped — transcribing…")
        elif isinstance(frame, TranscriptionFrame):
            print(f"    you: {frame.text.strip()!r}")
        elif isinstance(frame, LLMTextFrame):
            print(f"    claude: {frame.text!r}")
        elif isinstance(frame, TTSStartedFrame):
            self._tts_audio_bytes = 0
            print("    [tts started]")
        elif isinstance(frame, TTSStoppedFrame):
            print(f"    [tts stopped — synthesized {self._tts_audio_bytes} audio bytes]")
        elif isinstance(frame, TTSAudioRawFrame):
            self._tts_audio_bytes += len(frame.audio)
        await self.push_frame(frame, direction)


def _build_vad() -> SileroVADAnalyzer:
    return SileroVADAnalyzer(
        params=VADParams(
            confidence=0.8,
            start_secs=0.35,
            stop_secs=0.7,
            min_volume=0.0,
        )
    )


async def _run_pipeline_for_connection(connection: SmallWebRTCConnection) -> None:
    """One pipeline per call. Tears down when the client disconnects."""
    log = logging.getLogger("jarvis.pipeline")
    log.info("connection accepted pc_id=%s", connection.pc_id)

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # iOS WebRTC negotiates Opus at 48k. Let Pipecat pick sample rates.
        ),
    )
    stt = _build_stt()
    llm = ClaudeCodeLLMService(workspace=WORKSPACE)
    tts = _build_tts()

    log.info(
        "providers: stt=%s tts=%s", type(stt).__name__, type(tts).__name__
    )

    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=_build_vad()),
            stt,
            llm,
            tts,
            EventLogger(),
            transport.output(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        log.info("client disconnected; cancelling pipeline")
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        log.info("pipeline finished pc_id=%s", connection.pc_id)


app = FastAPI(title="Jarvis voice agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# SINGLE mode = one call at a time. This is a personal voice assistant; if a
# second client connects while a session is active the handler rejects it.
_request_handler = SmallWebRTCRequestHandler()


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    async def _on_new_connection(connection: SmallWebRTCConnection) -> None:
        background_tasks.add_task(_run_pipeline_for_connection, connection)

    return await _request_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=_on_new_connection,
    )


@app.patch("/api/offer")
async def offer_patch(request: SmallWebRTCPatchRequest):
    await _request_handler.handle_patch_request(request)
    return {"status": "success"}


# Browser test page for smoke-testing the WebRTC plumbing without Xcode.
# Mounted last so /api/* routes win when there's a name collision.
_web_dir = Path(__file__).parent / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    uvicorn.run(app, host=JARVIS_HOST, port=JARVIS_PORT, log_level="info")


if __name__ == "__main__":
    main()
