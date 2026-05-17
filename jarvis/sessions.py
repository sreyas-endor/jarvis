"""Multi-session manager.

Lets the voice (master) Claude Code session attach to other Claude Code
sessions running on the same Mac — including external sessions launched
from a terminal — and narrate their major events into the voice call.

Sessions live on disk as JSONL transcripts under
``~/.claude/projects/<dir-hash>/<session-uuid>.jsonl``. Each line is one
event. We tail the file forward from its current end, parse events
incrementally, filter down to the "major" ones, and hand them to the
narration callback for TTS.

We deliberately stick to read-only tailing: claude CLI sessions own
their own process; we can't safely inject input. The voice user gets
visibility into what other sessions are doing; sending instructions
back to them requires switching to the terminal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SESSIONS_ROOT = Path.home() / ".claude" / "projects"

# Throttle so a busy session can't flood the voice channel. One narration
# per session per N seconds; events in between are coalesced.
NARRATION_MIN_GAP_SECONDS: float = 2.0

# Tail loop polls the file size at this interval. Claude Code writes
# synchronously per event, so 250ms is responsive without being expensive.
TAIL_POLL_INTERVAL_SECONDS: float = 0.25


@dataclass
class SessionInfo:
    """Lightweight summary of a discoverable session on disk."""

    session_id: str
    project_dir: str          # decoded path from the dir-hash name
    transcript_path: Path
    last_modified: float
    first_user_message: str   # truncated; helps the user identify the session


@dataclass
class NarrationEvent:
    """One event worth surfacing to the user via voice."""

    session_id: str
    kind: str    # "tool_use" | "tool_error" | "assistant_text" | "user_msg"
    summary: str


def _decode_project_dir(dir_name: str) -> str:
    """Claude Code encodes a path by replacing slashes with dashes.

    e.g. ``-Users-nitesh-projects-jarvis-workspace`` ->
    ``/Users/nitesh/projects/jarvis/workspace``. Not bijective (a real
    dash in a path collides) but good enough for display.
    """
    return dir_name.replace("-", "/")


def _truncate(text: str, limit: int = 80) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _extract_first_user_message(path: Path) -> str:
    """Read the first ~50 lines looking for a real user message.

    Used to give the voice user a "what was this session about?" hint
    when listing sessions. Hook/system events at the top are skipped.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(80), f):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "user":
                    msg = ev.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        return _truncate(content)
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "text"
                                and isinstance(block.get("text"), str)
                            ):
                                return _truncate(block["text"])
                # Treat top-level "content" on queue-operation as a hint too —
                # that's what stream-json sometimes emits as the very first
                # user input.
                if ev.get("type") == "queue-operation" and ev.get("operation") == "enqueue":
                    c = ev.get("content")
                    if isinstance(c, str) and c.strip():
                        return _truncate(c)
    except OSError:
        return ""
    return ""


def discover_sessions(
    *,
    limit: int = 20,
    max_age_hours: float = 48.0,
    exclude_session_id: str | None = None,
) -> list[SessionInfo]:
    """List recent sessions across all projects, newest first.

    ``exclude_session_id`` keeps the master's own session out of the
    listing — claude would be confused by being able to attach to
    itself.
    """
    if not SESSIONS_ROOT.is_dir():
        return []

    out: list[SessionInfo] = []
    cutoff = time.time() - (max_age_hours * 3600)
    for project_dir in SESSIONS_ROOT.iterdir():
        if not project_dir.is_dir():
            continue
        for transcript in project_dir.glob("*.jsonl"):
            try:
                stat = transcript.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                continue
            sid = transcript.stem
            if exclude_session_id and sid == exclude_session_id:
                continue
            out.append(
                SessionInfo(
                    session_id=sid,
                    project_dir=_decode_project_dir(project_dir.name),
                    transcript_path=transcript,
                    last_modified=stat.st_mtime,
                    first_user_message="",  # filled lazily — heavy I/O for all sessions
                )
            )
    out.sort(key=lambda s: s.last_modified, reverse=True)
    out = out[:limit]
    for s in out:
        s.first_user_message = _extract_first_user_message(s.transcript_path)
    return out


def find_session(session_id: str) -> SessionInfo | None:
    """Look up a specific session by ID across all project dirs."""
    if not SESSIONS_ROOT.is_dir():
        return None
    for project_dir in SESSIONS_ROOT.iterdir():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            try:
                stat = candidate.stat()
            except OSError:
                return None
            return SessionInfo(
                session_id=session_id,
                project_dir=_decode_project_dir(project_dir.name),
                transcript_path=candidate,
                last_modified=stat.st_mtime,
                first_user_message=_extract_first_user_message(candidate),
            )
    return None


# Narration filtering ---------------------------------------------------------


def _summarize_event(session_id: str, ev: dict) -> NarrationEvent | None:
    """Pick out the user-interesting events from raw JSONL.

    Skips streaming text deltas, parent-uuid chatter, queue operations,
    and anything we don't have a short phrasing for. The voice user
    wants headlines, not a transcript.
    """
    ev_type = ev.get("type")

    # Inbound user message to the session being tailed — usually because
    # someone typed in that terminal.
    if ev_type == "user":
        msg = ev.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return NarrationEvent(
                session_id=session_id,
                kind="user_msg",
                summary=f"new message: {_truncate(content, 80)}",
            )
        # Skip tool_result-shaped user messages (those are just outputs
        # being fed back to claude).
        return None

    if ev_type == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        # First scan for tool_use — those are the "claude is doing X" events.
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool = block.get("name", "")
                inp = block.get("input") or {}
                return NarrationEvent(
                    session_id=session_id,
                    kind="tool_use",
                    summary=_phrase_tool_use(tool, inp),
                )
        # Otherwise, surface assistant text only if it looks like a turn
        # completion (final answer) — skip if claude is mid-tool-result.
        text_parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = " ".join(t for t in text_parts if t).strip()
        if text:
            return NarrationEvent(
                session_id=session_id,
                kind="assistant_text",
                summary=_truncate(text, 120),
            )
        return None

    return None


def _phrase_tool_use(tool: str, inp: dict) -> str:
    """One-line voice phrasing of a tool invocation."""
    if tool in ("Edit", "MultiEdit"):
        path = inp.get("file_path", "")
        return f"editing {_basename(path)}"
    if tool == "Write":
        return f"writing {_basename(inp.get('file_path', ''))}"
    if tool == "NotebookEdit":
        return f"editing notebook {_basename(inp.get('notebook_path', ''))}"
    if tool == "Bash":
        cmd = (inp.get("command") or "").strip().split("\n")[0]
        return f"running {_truncate(cmd, 60)}"
    if tool == "Read":
        return f"reading {_basename(inp.get('file_path', ''))}"
    if tool == "Grep":
        return f"searching for {_truncate(inp.get('pattern', ''), 40)}"
    if tool == "Glob":
        return f"listing {_truncate(inp.get('pattern', ''), 40)}"
    if tool == "Task":
        return f"spinning up a subagent: {_truncate(inp.get('description', ''), 50)}"
    return f"calling {tool}"


def _basename(path: str) -> str:
    if not path:
        return "a file"
    return os.path.basename(path) or path


# Tail loop -------------------------------------------------------------------


class SessionTailer:
    """Watches one transcript file and emits major events.

    Starts at end-of-file (we don't replay history). Polls for size
    changes; reads new lines; runs each through ``_summarize_event``.
    """

    def __init__(
        self,
        session: SessionInfo,
        on_event: Callable[[NarrationEvent], Awaitable[None]],
    ) -> None:
        self._session = session
        self._on_event = on_event
        self._task: asyncio.Task | None = None
        self._last_emit = 0.0

    @property
    def session_id(self) -> str:
        return self._session.session_id

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        path = self._session.transcript_path
        try:
            f = path.open("r", encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("tailer: can't open %s: %s", path, e)
            return
        try:
            f.seek(0, os.SEEK_END)
            pending: list[NarrationEvent] = []
            while True:
                line = f.readline()
                if not line:
                    if pending:
                        await self._maybe_emit(pending)
                    await asyncio.sleep(TAIL_POLL_INTERVAL_SECONDS)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                summary = _summarize_event(self._session.session_id, ev)
                if summary is not None:
                    pending.append(summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tailer crashed for session %s", self._session.session_id)
        finally:
            try:
                f.close()
            except Exception:
                pass

    async def _maybe_emit(self, pending: list[NarrationEvent]) -> None:
        """Throttle: emit at most one event per NARRATION_MIN_GAP_SECONDS.

        When multiple events accumulate inside one gap window we keep the
        *last* one (most recent state) and drop the rest. Headline beats
        a stale running commentary.
        """
        now = time.monotonic()
        if now - self._last_emit < NARRATION_MIN_GAP_SECONDS:
            # Hold for next tick. Trim list to bounded size so a stuck
            # consumer doesn't grow memory.
            del pending[:-5]
            return
        event = pending[-1]
        pending.clear()
        self._last_emit = now
        try:
            await self._on_event(event)
        except Exception:
            log.exception("narration callback raised")


class SessionRegistry:
    """Track which sessions are currently attached for narration."""

    def __init__(
        self,
        on_event: Callable[[NarrationEvent], Awaitable[None]],
    ) -> None:
        self._on_event = on_event
        self._tailers: dict[str, SessionTailer] = {}
        self._lock = asyncio.Lock()

    @property
    def attached_ids(self) -> list[str]:
        return list(self._tailers.keys())

    async def attach(self, session_id: str) -> SessionInfo | None:
        async with self._lock:
            if session_id in self._tailers:
                # Already attached. Return its info so callers can confirm.
                info = find_session(session_id)
                return info
            info = find_session(session_id)
            if info is None:
                return None
            tailer = SessionTailer(info, self._on_event)
            tailer.start()
            self._tailers[session_id] = tailer
            return info

    async def detach(self, session_id: str) -> bool:
        async with self._lock:
            tailer = self._tailers.pop(session_id, None)
        if tailer is None:
            return False
        await tailer.stop()
        return True

    async def detach_all(self) -> None:
        async with self._lock:
            tailers = list(self._tailers.values())
            self._tailers.clear()
        for t in tailers:
            await t.stop()
