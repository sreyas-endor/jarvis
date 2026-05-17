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
import random
import re
from pathlib import Path

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

from .event_bridge import (
    PermissionRequest,
    ToolUseStart,
    TurnComplete,
    events_to_frames,
)
from .ndjson_parser import parse_line
from .streaming import ClaudeStreamingProcess

log = logging.getLogger(__name__)

# File + shell + agent tools. Excludes WebFetch/WebSearch and MCP — those
# add network egress or unknown third-party side effects that aren't worth
# the voice-prompt overhead.
DEFAULT_TOOLS: list[str] = [
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash",
    "Task",
    "TodoWrite",
]

# Read-only / harmless tools — run without asking. Anything else in
# DEFAULT_TOOLS triggers a voice permission prompt before claude executes.
DEFAULT_ALLOWED_TOOLS: list[str] = [
    "Read",
    "Glob",
    "Grep",
    "TodoWrite",
]

# How long we wait for a yes/no after asking. If the user is silent the
# request is denied — better than leaving claude blocked forever and
# wedging the call.
PERMISSION_RESPONSE_TIMEOUT_SECONDS: float = 30.0

# Regex-based yes/no parser for the user's spoken reply. Match the start
# of the utterance — "yes please" → yes, "no don't" → no. Ambiguous
# replies fall through and re-prompt once.
_YES_RE = "^(yes|yeah|yep|sure|go|allow|do it|approved?|okay|ok)\\b"
_NO_RE = "^(no|nope|nah|deny|stop|cancel|don'?t|negative)\\b"

# Spoken filler phrases injected on the first ToolUseStart per turn so the
# user hears Jarvis acknowledging work instead of dead air during the
# Claude-thinks-then-tool-runs window. Verb has to match the action — saying
# "let me check" while writing a file is a lie. TodoWrite is intentionally
# absent (internal bookkeeping, sub-second). One filler per turn — chained
# tools stay silent after; the user already knows we're working.
FILLERS: dict[str, list[str]] = {
    "Read": [
        "let me check…",
        "hmm, pulling that up…",
        "okay, looking…",
        "lemme take a peek…",
        "alright, checking…",
        "give me a moment to look…",
    ],
    "Grep": [
        "searching for that…",
        "let me find it…",
        "hunting that down…",
        "okay, scanning…",
        "hmm, digging around…",
        "let me grep for it…",
    ],
    "Glob": [
        "searching for that…",
        "let me find it…",
        "okay, scanning the tree…",
        "hmm, looking around…",
        "let me see what's there…",
    ],
    "Bash": [
        "running that real quick…",
        "okay, on it…",
        "let me run that…",
        "hold on a moment…",
        "alright, kicking that off…",
        "hmm, checking…",
    ],
    "Edit": [
        "okay, updating that…",
        "making the change…",
        "got it, editing now…",
        "alright, fixing that up…",
        "hmm, patching that…",
        "yep, on it…",
    ],
    "NotebookEdit": [
        "okay, updating that…",
        "making the change…",
        "got it, editing now…",
        "alright, on it…",
    ],
    "Write": [
        "writing that out…",
        "okay, putting that down…",
        "creating that file…",
        "alright, on it…",
        "got it, writing now…",
    ],
    "Task": [
        "got it, on it…",
        "spinning that up…",
        "okay, delegating that…",
        "alright, kicking that off…",
        "hmm, handing that off…",
    ],
}

# Fallback for tools we don't have a specific filler for (e.g. "Agent",
# future Claude Code tools, MCP tools). Better to say something generic
# than stay silent.
GENERIC_FILLERS: list[str] = [
    "okay, on it…",
    "let me check…",
    "hmm, one moment…",
    "give me a sec…",
    "alright, looking into that…",
]

# Heartbeat phrases for tool runs that stretch past HEARTBEAT_INTERVAL.
# Played periodically while a tool is still executing and claude hasn't
# yet emitted any spoken text, so the user knows jarvis is alive instead
# of frozen.
HEARTBEAT_FILLERS: list[str] = [
    "still on it…",
    "hmm, still working…",
    "almost there…",
    "okay, give me another sec…",
    "still digging…",
    "yep, still going…",
]

HEARTBEAT_INTERVAL_SECONDS: float = 10.0

# Immediate acknowledgement spoken the moment we receive a transcription,
# before claude has even seen the message. Cuts perceived latency: instead
# of waiting ~2s in dead air for claude's TTFT + TTS startup, the user hears
# us responding within ~200ms. Picked to be short, low-pitched, and sound
# like a real "I'm listening" backchannel rather than canned filler.
ACK_FILLERS: list[str] = [
    "mhm…",
    "yeah…",
    "okay…",
    "got it…",
    "right…",
    "hmm…",
]


class ClaudeCodeLLMService(LLMService):
    def __init__(
        self,
        *,
        workspace: Path,
        model: str = "claude-haiku-4-5",
        tools: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        add_dirs: list[Path] | None = None,
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
        # permission-mode=default makes claude actually emit control_request
        # for tools that aren't in allowed_tools, so we can voice-prompt the
        # user.
        self._proc = ClaudeStreamingProcess(
            model=model,
            workspace=workspace,
            tools=tools if tools is not None else DEFAULT_TOOLS,
            allowed_tools=(
                allowed_tools if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS
            ),
            permission_mode="default",
            add_dirs=add_dirs,
        )
        self._started = False
        self._event_task: asyncio.Task | None = None

        self._suppress_text_until_next_send = False  # user is mid-utterance
        self._awaiting_response = False  # sent to claude, waiting for its Start
        self._filler_injected_this_turn = False
        self._heartbeat_task: asyncio.Task | None = None

        # Permission gating: when claude requests permission for an
        # unlisted tool, we set _pending_permission to the request_id and
        # speak the prompt. The next non-empty TranscriptionFrame is parsed
        # as yes/no rather than forwarded as a user message.
        self._pending_permission: PermissionRequest | None = None
        self._permission_timeout_task: asyncio.Task | None = None

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
            self._cancel_heartbeat()
            await self.push_frame(InterruptionFrame())
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterruptionFrame):
            self._suppress_text_until_next_send = True
            self._cancel_heartbeat()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            text = frame.text.strip()
            # If claude is blocked waiting on a permission decision, this
            # utterance is the user's yes/no — route it there instead of
            # treating it as a new chat message.
            if self._pending_permission is not None:
                self._suppress_text_until_next_send = False
                await self._handle_permission_reply(text)
                return

            await self._ensure_started()
            self._suppress_text_until_next_send = False
            self._awaiting_response = True
            # Speak the ack *before* sending to claude so the user hears
            # "mhm…" within ~200ms of finishing their utterance instead of
            # waiting for the model's TTFT. TTSSpeakFrame bypasses the
            # sentence aggregator (see _maybe_inject_filler) so it
            # synthesizes as an independent utterance.
            await self.push_frame(
                TTSSpeakFrame(
                    text=random.choice(ACK_FILLERS),
                    append_to_context=False,
                )
            )
            await self._proc.send_user(text)
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
                    await self._maybe_inject_filler(item.name)
                elif isinstance(item, PermissionRequest):
                    await self._handle_permission_request(item)
                elif isinstance(item, TurnComplete):
                    log.info("turn complete: stop=%s", item.stop_reason)
                    self._filler_injected_this_turn = False
                    self._cancel_heartbeat()
                elif isinstance(item, LLMFullResponseStartFrame):
                    self._awaiting_response = False
                    await self.push_frame(item)
                elif isinstance(item, LLMTextFrame):
                    if (
                        not self._suppress_text_until_next_send
                        and not self._awaiting_response
                    ):
                        await self.push_frame(item)
                        # Claude is speaking now — heartbeat's job is done.
                        self._cancel_heartbeat()
                else:
                    # LLMFullResponseEndFrame and any other structural frame —
                    # always flow so Pipecat's response state stays balanced.
                    await self.push_frame(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event loop crashed")

    async def _maybe_inject_filler(self, tool_name: str) -> None:
        # Skip if we already injected this turn or user is mid-speech.
        # Tool-specific fillers when known; generic fallback otherwise so
        # unknown tools (Agent, future MCP tools, etc.) still get a voice.
        # We use TTSSpeakFrame rather than LLMTextFrame because the sentence
        # aggregator buffers text behind a non-whitespace-lookahead gate
        # (see SimpleTextAggregator); when claude pauses for a tool, the
        # lookahead never arrives and the filler would sit in the buffer
        # until the tool finishes. TTSSpeakFrame bypasses aggregation and
        # synthesizes immediately as an independent utterance.
        if self._filler_injected_this_turn:
            return
        if self._suppress_text_until_next_send:
            return
        fillers = FILLERS.get(tool_name) or GENERIC_FILLERS
        self._filler_injected_this_turn = True
        await self.push_frame(
            TTSSpeakFrame(text=random.choice(fillers), append_to_context=False)
        )
        self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        # Idempotent — only one heartbeat per turn. Cancelled when claude
        # finally speaks, when the turn completes, or when the user starts
        # talking.
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if self._suppress_text_until_next_send:
                    return
                await self.push_frame(
                    TTSSpeakFrame(
                        text=random.choice(HEARTBEAT_FILLERS),
                        append_to_context=False,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat loop crashed")

    async def _handle_permission_request(self, req: PermissionRequest) -> None:
        """Speak the prompt + arm yes/no routing for the next transcription."""
        # If another prompt is already in flight, auto-deny the new one.
        # Better than queueing — the user only tracks one thing at a time on
        # a call.
        if self._pending_permission is not None:
            log.warning(
                "received permission request for %s while already waiting on %s; auto-denying",
                req.tool,
                self._pending_permission.tool,
            )
            await self._proc.send_control_response(
                req.request_id, allow=False, message="busy with another prompt"
            )
            return

        self._pending_permission = req
        # Heartbeat would step on the prompt — cancel it. Tool filler flag
        # cleared so the next genuine tool start can still announce itself.
        self._cancel_heartbeat()
        self._filler_injected_this_turn = True

        prompt = self._format_permission_prompt(req)
        log.info("voice permission prompt: %s", prompt)
        await self.push_frame(TTSSpeakFrame(text=prompt, append_to_context=False))

        # Safety net so a silent user doesn't strand claude.
        self._permission_timeout_task = asyncio.create_task(
            self._permission_timeout()
        )

    def _format_permission_prompt(self, req: PermissionRequest) -> str:
        """Tool-specific phrasing. Keep it short — the user's on a call."""
        tool = req.tool
        args = req.args or {}
        if tool in ("Edit", "Write", "NotebookEdit"):
            target = args.get("file_path") or args.get("notebook_path") or "a file"
            verb = "edit" if tool == "Edit" else "write to"
            return f"I want to {verb} {self._friendly_path(target)}. Okay?"
        if tool == "Bash":
            cmd = (args.get("command") or "").strip()
            if len(cmd) > 80:
                cmd = cmd[:77] + "…"
            return f"I want to run: {cmd}. Okay?"
        if tool == "Task":
            return "I want to spin up a subagent. Okay?"
        return f"I want to use the {tool} tool. Okay?"

    @staticmethod
    def _friendly_path(p: str) -> str:
        """Strip workspace dirs and read out just a tail path."""
        # Path is read out by TTS, so prefer short — keep just the last two
        # segments which is usually enough to disambiguate.
        parts = [s for s in p.split("/") if s]
        return "/".join(parts[-2:]) if len(parts) > 2 else p

    async def _permission_timeout(self) -> None:
        try:
            await asyncio.sleep(PERMISSION_RESPONSE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        req = self._pending_permission
        if req is None:
            return
        log.warning("permission prompt timed out for %s; denying", req.tool)
        self._pending_permission = None
        await self.push_frame(
            TTSSpeakFrame(text="No answer — skipping that.", append_to_context=False)
        )
        await self._proc.send_control_response(
            req.request_id, allow=False, message="user did not respond"
        )

    async def _handle_permission_reply(self, text: str) -> None:
        """Parse a yes/no out of the user's reply and forward the verdict."""
        req = self._pending_permission
        if req is None:
            return  # raced with timeout — nothing to do
        lower = text.lower().strip()
        if re.match(_YES_RE, lower):
            allow = True
        elif re.match(_NO_RE, lower):
            allow = False
        else:
            # Ambiguous reply — re-prompt once. Treat the user's words as
            # extra context to feed claude on the next turn if they keep
            # ducking the question; for now just ask again.
            log.info("ambiguous permission reply %r; re-prompting", text)
            await self.push_frame(
                TTSSpeakFrame(
                    text="Sorry — yes or no?",
                    append_to_context=False,
                )
            )
            return

        self._pending_permission = None
        if self._permission_timeout_task and not self._permission_timeout_task.done():
            self._permission_timeout_task.cancel()
        self._permission_timeout_task = None
        await self._proc.send_control_response(req.request_id, allow=allow)
        # Short verbal acknowledgement so the user knows their reply landed,
        # then claude's next stream emits whatever it does post-decision.
        await self.push_frame(
            TTSSpeakFrame(
                text="okay" if allow else "skipping that",
                append_to_context=False,
            )
        )

    async def cleanup(self) -> None:
        self._cancel_heartbeat()
        if (
            self._permission_timeout_task is not None
            and not self._permission_timeout_task.done()
        ):
            self._permission_timeout_task.cancel()
        if self._event_task is not None and not self._event_task.done():
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        await self._proc.stop()
        await super().cleanup()
