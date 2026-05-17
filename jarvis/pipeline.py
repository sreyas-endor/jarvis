"""Pipeline assembly — wires transport, VAD, STT, LLM, TTS, and logging.

This module owns the "shape" of the conversation: which Pipecat processors
run in which order, and how user-facing events are surfaced. Provider
choice is delegated to jarvis.stt.build_stt and jarvis.tts.build_tts.

Two entry points:
  - ``run()`` boots the local-audio pipeline (mic + speakers on this Mac).
    Useful for dev on the laptop without the phone.
  - ``build_pipeline_task(transport)`` returns a configured ``PipelineTask``
    bound to whatever ``BaseTransport`` you hand it (e.g. a per-call
    ``SmallWebRTCTransport``). Lets ``main.py`` host the WebRTC server.
"""
from __future__ import annotations

import logging
from pathlib import Path

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
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from jarvis.llm import ClaudeCodeLLMService
from jarvis.stt import build_stt
from jarvis.tts import build_tts

WORKSPACE = Path(__file__).parent.parent / "workspace"

# User's existing Claude Code auto-memory pool (built up over normal monorepo
# sessions). Read-only context for Jarvis — the persona prompt in
# workspace/CLAUDE.md tells the model not to write here. Jarvis writes its
# own session memory to ~/.claude/projects/-Users-ss-Code-jarvis-workspace/
# automatically (separate pool, managed by Claude Code's auto-memory).
USER_MEMORY_DIR = (
    Path.home() / ".claude" / "projects" / "-Users-ss-Code-monorepo" / "memory"
)

log = logging.getLogger(__name__)


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
    # VAD must be wired as a pipeline processor in Pipecat 1.1.0 — the
    # vad_analyzer kwarg on transport params is silently dropped by Pydantic.
    # Volume gate disabled (min_volume=0) because the MacBook internal mic
    # peaks ~0.1-0.15 of full scale; Silero handles speech detection on its
    # own.
    return SileroVADAnalyzer(
        params=VADParams(
            confidence=0.8,
            start_secs=0.35,
            stop_secs=0.7,
            min_volume=0.0,
        )
    )


def build_pipeline_task(transport: BaseTransport) -> PipelineTask:
    """Wire VAD + STT + LLM + TTS + logger around the supplied transport.

    Same pipeline shape regardless of whether the audio I/O sits on the Mac
    (LocalAudioTransport, for laptop dev) or on the phone over WebRTC
    (SmallWebRTCTransport, for the iOS app).
    """
    stt = build_stt()
    log.info("STT provider: %s", type(stt).__name__)

    # Surface the user's monorepo memory pool as read-only context if it
    # exists locally; skip silently on machines where it doesn't.
    extra_dirs = [USER_MEMORY_DIR] if USER_MEMORY_DIR.is_dir() else []
    if extra_dirs:
        log.info("Mounting user memory: %s", USER_MEMORY_DIR)
    llm = ClaudeCodeLLMService(workspace=WORKSPACE, add_dirs=extra_dirs)

    tts = build_tts()
    log.info("TTS provider: %s", type(tts).__name__)

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
    return PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )


async def run() -> None:
    """Boot the local-audio pipeline (mic + speakers on this Mac)."""
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )
    task = build_pipeline_task(transport)
    await PipelineRunner().run(task)
