"""Pipecat LLMService driving a long-running Claude Code stream-json process.

One persistent `claude -p --input-format stream-json` subprocess handles every
turn in the conversation. User utterances are written to its stdin as JSON
lines; assistant tokens stream back on stdout and become Pipecat frames.

Per-turn cost after warmup: ~1-1.5s (vs ~6s for per-turn spawn). The hang bug
that originally pushed us to per-turn (GH #3187) was Windows-only and closed.

Barge-in: text frames are gated by two flags.
  `_suppress_text_until_next_send`: True while user is speaking (set on
    VADUserStartedSpeakingFrame, cleared when transcription arrives). Covers
    the in-flight-tail-text window.
  `_awaiting_response`: True after we send to claude, False on the next
    LLMFullResponseStartFrame. Holds back text frames until claude has
    started responding to our most recent send — robust to claude
    consolidating multiple rapid user messages into one response.
Structural Start/End frames always flow so Pipecat's response state stays
balanced.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

from .claude_streaming import ClaudeStreamingProcess
from .event_bridge import (
    PermissionRequest,
    ToolUseStart,
    TurnComplete,
    events_to_frames,
)
from .ndjson_parser import parse_line

log = logging.getLogger(__name__)

DEFAULT_TOOLS: list[str] = ["Read"]  # --tools "Read" -> file reading only


class ClaudeCodeLLMService(LLMService):
    def __init__(
        self,
        *,
        workspace: Path,
        model: str = "claude-haiku-4-5",
        tools: list[str] | None = None,
    ) -> None:
        # Claude Code owns generation parameters (model, temperature, system prompt, etc.)
        # via CLI flags and its own CLAUDE.md, so we surface only the model name to Pipecat
        # and leave the rest unset rather than NOT_GIVEN.
        settings = LLMSettings(
            model=model,
            system_instruction=None,
            temperature=None,
            max_tokens=None,
            top_p=None,
            top_k=None,
            frequency_penalty=None,
            presence_penalty=None,
            seed=None,
            filter_incomplete_user_turns=None,
            user_turn_completion_config=None,
        )
        super().__init__(settings=settings)
        self._proc = ClaudeStreamingProcess(
            model=model,
            workspace=workspace,
            tools=tools if tools is not None else DEFAULT_TOOLS,
        )
        self._started = False
        self._event_task: asyncio.Task | None = None

        self._suppress_text_until_next_send = False  # user is mid-utterance
        self._awaiting_response = False  # sent to claude, waiting for its Start

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            # VADProcessor emits VADUserStartedSpeakingFrame (sibling of, not
            # subclass of, UserStartedSpeakingFrame). Pipecat 1.1.0 doesn't
            # auto-emit InterruptionFrame on VAD events, so the bot keeps
            # speaking over the user unless we wire barge-in ourselves.
            # Suppress our own text output and push an InterruptionFrame
            # downstream so TTS flushes and the output transport drains its
            # already-queued audio.
            self._suppress_text_until_next_send = True
            await self.push_frame(InterruptionFrame())
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterruptionFrame):
            self._suppress_text_until_next_send = True
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            await self._ensure_started()
            self._suppress_text_until_next_send = False
            self._awaiting_response = True
            await self._proc.send_user(frame.text.strip())
            return

        await self.push_frame(frame, direction)

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        await self._proc.start()
        self._event_task = asyncio.create_task(self._event_loop())

    async def _event_loop(self) -> None:
        async def event_stream():
            async for line in self._proc.lines():
                ev = parse_line(line)
                if ev is not None:
                    yield ev

        try:
            async for item in events_to_frames(event_stream()):
                if isinstance(item, ToolUseStart):
                    log.info("claude tool use start: %s", item.name)
                elif isinstance(item, PermissionRequest):
                    log.warning(
                        "permission requested for %s (auto-deny; voice prompt TBD)",
                        item.tool,
                    )
                elif isinstance(item, TurnComplete):
                    log.info("turn complete: stop=%s", item.stop_reason)
                elif isinstance(item, LLMFullResponseStartFrame):
                    self._awaiting_response = False
                    await self.push_frame(item)
                elif isinstance(item, LLMTextFrame):
                    if (
                        not self._suppress_text_until_next_send
                        and not self._awaiting_response
                    ):
                        await self.push_frame(item)
                else:
                    # LLMFullResponseEndFrame and any other structural frame —
                    # always flow so Pipecat's response state stays balanced.
                    await self.push_frame(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event loop crashed")

    async def cleanup(self) -> None:
        if self._event_task is not None and not self._event_task.done():
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        await self._proc.stop()
        await super().cleanup()
