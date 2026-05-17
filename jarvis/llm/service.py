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

# How long the voice-permission prompt waits for a yes/no before it
# gives up and tells the PreToolUse hook to deny. The hook's HTTP
# timeout is slightly longer (see tools/voice_permission_hook.py) so
# the deny verdict travels cleanly instead of racing the HTTP request.
PERMISSION_VOICE_TIMEOUT_SECONDS: float = 45.0

# Regex match against the start of the user's spoken reply. "yes please"
# → yes; "no don't" → no. Ambiguous replies re-prompt once.
_YES_RE = r"^(yes|yeah|yep|yup|sure|go|do it|allow|approved?|okay|ok|fine)\b"
_NO_RE = r"^(no|nope|nah|don'?t|deny|stop|cancel|negative|abort)\b"

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
    # The voice-permission HTTP endpoint needs to find whichever LLM
    # service is driving the current call so it can speak the prompt and
    # collect the yes/no. SINGLE-call mode (one call at a time) means a
    # single class-level handle is fine — replaced on each new pipeline,
    # cleared on cleanup.
    _current_instance: "ClaudeCodeLLMService | None" = None

    @classmethod
    def active_instance(cls) -> "ClaudeCodeLLMService | None":
        return cls._current_instance

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
        # Claude Code stream-json mode does NOT emit control_request events
        # for permission prompts — that's an interactive-TTY feature. In
        # non-interactive mode the model either auto-runs (when the tool is
        # in --allowedTools) or silently blocks and *talks* about needing
        # permission in its text output, with no programmatic hook for us
        # to grab. So we pre-permit everything and treat the voice call
        # itself as the supervision channel: the user hears every tool
        # filler before claude acts, and can interrupt mid-stream.
        #
        # A future "true" voice-prompted permission router would have to
        # run as a custom MCP tool, not via control_request. Out of scope
        # for now.
        self._proc = ClaudeStreamingProcess(
            model=model,
            workspace=workspace,
            tools=tools if tools is not None else DEFAULT_TOOLS,
            allowed_tools=allowed_tools,  # None -> pre-permit everything in `tools`
            add_dirs=add_dirs,
        )
        self._started = False
        self._event_task: asyncio.Task | None = None

        self._suppress_text_until_next_send = False  # user is mid-utterance
        self._awaiting_response = False  # sent to claude, waiting for its Start
        self._filler_injected_this_turn = False
        self._heartbeat_task: asyncio.Task | None = None

        # Voice-permission state. _pending_permission_future is set while
        # we're waiting on a yes/no from the user — the next non-empty
        # TranscriptionFrame is routed to it instead of being forwarded
        # to claude as a chat message.
        self._pending_permission_future: asyncio.Future[dict] | None = None

        type(self)._current_instance = self

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
            # If claude is blocked on a PreToolUse hook waiting for a
            # yes/no, this utterance is the verdict — route it to the
            # pending future instead of sending it to claude as a new
            # chat message.
            if self._pending_permission_future is not None and not self._pending_permission_future.done():
                self._suppress_text_until_next_send = False
                await self._resolve_permission_reply(text)
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
                    # Claude Code stream-json doesn't actually emit these
                    # for tool prompts (the model just talks about needing
                    # permission instead). Logging here in case a future
                    # CLI version starts surfacing them so we know to wire
                    # a router back in.
                    log.warning(
                        "received unexpected control_request for %s; ignoring",
                        item.tool,
                    )
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

    async def request_permission_voice(self, *, tool: str, args: dict) -> dict:
        """Speak a permission prompt and wait for a yes/no.

        Called by the FastAPI ``/internal/permission`` endpoint when the
        PreToolUse hook in workspace/.claude/settings.local.json fires.
        Returns ``{"allow": bool, "reason": str?}`` — the same shape the
        hook script consumes.
        """
        if (
            self._pending_permission_future is not None
            and not self._pending_permission_future.done()
        ):
            # Two concurrent prompts is a recipe for confusion on a voice
            # call. Refuse the second one so the user only tracks one
            # decision at a time.
            log.warning(
                "voice permission requested for %s while another prompt is active; auto-denying",
                tool,
            )
            return {"allow": False, "reason": "another prompt already pending"}

        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending_permission_future = future

        # Heartbeat would step on the prompt. The tool filler flag is set
        # so the upstream "creating that file…" filler doesn't fire over
        # the prompt either.
        self._cancel_heartbeat()
        self._filler_injected_this_turn = True

        prompt = self._format_permission_prompt(tool, args)
        log.info("voice permission prompt for %s: %s", tool, prompt)
        await self.push_frame(TTSSpeakFrame(text=prompt, append_to_context=False))

        try:
            return await asyncio.wait_for(future, PERMISSION_VOICE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.warning("voice permission for %s timed out", tool)
            await self.push_frame(
                TTSSpeakFrame(text="No answer — skipping that.", append_to_context=False)
            )
            return {"allow": False, "reason": "user did not respond"}
        finally:
            # Clear the slot unconditionally so a future timeout->reply
            # race doesn't leave a stale future pointer.
            if self._pending_permission_future is future:
                self._pending_permission_future = None

    async def _resolve_permission_reply(self, text: str) -> None:
        future = self._pending_permission_future
        if future is None or future.done():
            return
        lower = text.lower().strip()
        if re.match(_YES_RE, lower):
            future.set_result({"allow": True})
            await self.push_frame(
                TTSSpeakFrame(text="okay, on it", append_to_context=False)
            )
        elif re.match(_NO_RE, lower):
            future.set_result({"allow": False, "reason": "user said no"})
            await self.push_frame(
                TTSSpeakFrame(text="skipping that", append_to_context=False)
            )
        else:
            # Ambiguous reply — re-prompt once. The user's words aren't
            # forwarded to claude either way; they were spoken in the
            # permission context.
            log.info("ambiguous permission reply %r; re-prompting", text)
            await self.push_frame(
                TTSSpeakFrame(text="sorry, yes or no?", append_to_context=False)
            )

    def _format_permission_prompt(self, tool: str, args: dict) -> str:
        """Tool-specific phrasing. Short — the user's on a call."""
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            target = (
                args.get("file_path")
                or args.get("notebook_path")
                or "a file"
            )
            verb = "edit" if tool in ("Edit", "MultiEdit") else "write to"
            return f"I want to {verb} {self._friendly_path(target)}. Okay?"
        if tool == "Bash":
            cmd = (args.get("command") or "").strip()
            if len(cmd) > 100:
                cmd = cmd[:97] + "…"
            return f"I want to run: {cmd}. Okay?"
        if tool == "Task":
            description = args.get("description") or "a subtask"
            return f"I want to spin up a subagent for {description}. Okay?"
        return f"I want to use the {tool} tool. Okay?"

    @staticmethod
    def _friendly_path(p: str) -> str:
        # Path is read out by TTS; tail-only keeps it short.
        parts = [s for s in p.split("/") if s]
        return "/".join(parts[-2:]) if len(parts) > 2 else p

    async def cleanup(self) -> None:
        self._cancel_heartbeat()
        if self._pending_permission_future is not None and not self._pending_permission_future.done():
            self._pending_permission_future.cancel()
        if type(self)._current_instance is self:
            type(self)._current_instance = None
        if self._event_task is not None and not self._event_task.done():
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        await self._proc.stop()
        await super().cleanup()
