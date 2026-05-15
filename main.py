"""Jarvis voice agent — mic in, Claude Code as brain, voice out.

Pipeline: LocalAudioTransport → VAD → STT → ClaudeCodeLLMService → TTS →
EventLogger → LocalAudioTransport. STT and TTS providers are swappable
via STT_PROVIDER and TTS_PROVIDER env vars; see README for the matrix.

Run with AirPods or wired headphones to avoid the laptop-speaker echo loop.
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

# Load .env (e.g. CARTESIA_API_KEY) before pipecat services try to read env.
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

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
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from jarvis.azure_openai_tts_service import AzureOpenAITTSService
from jarvis.azure_phraselist_stt_service import AzurePhraseListSTTService
from jarvis.claude_code_llm_service import ClaudeCodeLLMService
from jarvis.whisper_jargon_stt_service import WhisperJargonSTTService

# Cartesia "Skylar - Friendly Guide" voice (resolved from
# https://api.cartesia.ai/voices on 2026-05-14).
SKYLAR_VOICE_ID = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
# Azure Speech default voice — multilingual neural voices are noticeably more
# natural than the older Aria/Jenny/Sara line. Ava is widely regarded as
# Azure's most natural conversational female.
AZURE_DEFAULT_VOICE = "en-US-AvaMultilingualNeural"
# Tuned to approximate Cartesia Skylar's character: warm/approachable
# customer-care female. We push style intensity but keep pitch at default —
# Ava's natural range is already on the higher side; lifting it reads
# squeaky.
#   style="friendly"      — warmer than "chat" (casual) or "customerservice" (polished)
#   style_degree=1.5      — push expressivity without cartoonish-ness (1.0=neutral, 2.0=max)
#   pitch=None            — no prosody pitch shift; override via AZURE_SPEECH_PITCH if needed
AZURE_DEFAULT_STYLE = "friendly"
AZURE_DEFAULT_STYLE_DEGREE = "1.5"
AZURE_DEFAULT_PITCH: str | None = None

# OpenAI TTS (via Azure AI Foundry) defaults — targeting Cartesia Skylar's
# warm conversational character. gpt-4o-mini-tts is the only OpenAI TTS
# model that supports the `instructions` field; tts-hd and tts ignore it
# but still use the voice setting.
#   fable — explicitly described as "warm, storytelling"; closest literal
#           match to Skylar's character in the OpenAI voice set
#   instructions — community findings (community.openai.com forums) say the
#                  model handles ONE concrete persona much better than a
#                  stack of adjectives. Short single-persona prompts beat
#                  long descriptive ones. Edit in-place to tune delivery;
#                  not overridable via env (intentional — keep the prompt
#                  in code where it's reviewable alongside voice selection).
#   N.B. There's a known regression in the current model alias — pinning
#   the Foundry deployment to model version 2025-03-20 restores stronger
#   instruction-following and naturalness.
OPENAI_DEFAULT_DEPLOYMENT = "gpt-4o-mini-tts"
OPENAI_DEFAULT_API_VERSION = "2024-10-01-preview"
OPENAI_DEFAULT_VOICE = "fable"
OPENAI_DEFAULT_SPEED = 1.0
OPENAI_DEFAULT_INSTRUCTIONS = (
    "Speak like a warm, engaged podcast host chatting with a close friend. "
    "Natural conversational pace, genuine smile in your voice."
)

# Microsoft MAI-Voice-1 — Microsoft's flagship expressive TTS, Aug 2025.
# Uses the standard Azure Speech SDK/REST API (same key + region as
# AZURE_SPEECH_KEY / AZURE_SPEECH_REGION). Voice name format is
# "{base-voice}:MAI-Voice-1". Roster:
#   en-US-Jasper:MAI-Voice-1   (male, expressive)
#   en-US-June:MAI-Voice-1     (female, warm-conversational) ← default
#   en-US-Grant:MAI-Voice-1    (male)
#   en-US-Iris:MAI-Voice-1     (female)
#   en-US-Reed:MAI-Voice-1     (male)
#   en-US-Joy:MAI-Voice-1      (female, upbeat)
# Override via MAI_VOICE env var.
#
# Caveats:
#   - Public preview; only available in select Azure regions
#   - $22/1M chars (~50x standard Azure Speech, but cheap for personal use)
#   - No SSML style override here — MAI voices are natively expressive
MAI_DEFAULT_VOICE = "en-US-June:MAI-Voice-1"

# Single source of truth for jargon biasing — used by both the Azure
# phrase-list STT (acoustic-model biasing) and the Whisper fallback
# (LM-prompt biasing). Put highest-value terms early; Whisper truncates
# past ~224 tokens, Azure phrase lists have no equivalent cap.
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


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"{name} missing. Set it in ~/Code/jarvis/.env")
    return val


def _build_stt():
    """Pick an STT service based on STT_PROVIDER env var (default: azure).

    azure   — Azure Speech with PhraseListGrammar attached. Acoustic-layer
              biasing toward JARGON_PHRASES; strictly stronger than Whisper's
              LM-prompt biasing for known-vocabulary cases. Cloud.
    whisper — Local Whisper Large V3 Turbo Q4 with initial_prompt biasing
              (WhisperJargonSTTService). No network needed.
    """
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
    """Pick a TTS service based on TTS_PROVIDER env var (default: cartesia)."""
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
        # MAI-Voice-1 runs on the same Azure Speech service — same key/region,
        # different voice name. No SSML style overrides; the model is natively
        # expressive.
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

WORKSPACE = Path(__file__).parent / "workspace"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # VAD must be wired as a pipeline processor in Pipecat 1.1.0.
    # vad_analyzer is not a valid TransportParams field — Pydantic silently
    # drops the kwarg, so attaching it to LocalAudioTransportParams is a no-op.
    # Volume gate disabled because the MacBook internal mic peaks around
    # 0.1-0.15 of full scale; Silero's model handles speech detection on its own.
    vad = SileroVADAnalyzer(
        params=VADParams(
            confidence=0.8,
            start_secs=0.35,
            stop_secs=0.7,
            min_volume=0.0,
        )
    )
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )

    stt = _build_stt()
    logging.info("STT provider: %s", type(stt).__name__)
    llm = ClaudeCodeLLMService(workspace=WORKSPACE)
    tts = _build_tts()
    logging.info("TTS provider: %s", type(tts).__name__)

    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=vad),
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

    await PipelineRunner().run(task)


if __name__ == "__main__":
    asyncio.run(main())
