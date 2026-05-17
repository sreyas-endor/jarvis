"""Long-running `claude -p` subprocess for low-latency voice turns.

One process is spawned on start() and reused across turns. User utterances
are written as stream-json lines to stdin; assistant events come back on
stdout. Eliminates the ~5s per-turn cold start of spawning fresh.

Bug #3187 (input-stream-json hang) was Windows-only and is closed; verified
clean on macOS / claude 2.1.x.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)

STDIN_FLUSH_TIMEOUT = 5.0
SHUTDOWN_TIMEOUT = 3.0
# Claude's stream-json output can emit a single NDJSON line >64KB (asyncio's
# default readline limit) when a tool result embeds large file contents.
# Bump to 10MB so Read of a normal source file doesn't kill the reader.
STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024


class ClaudeStreamingProcess:
    def __init__(
        self,
        *,
        model: str,
        workspace: Path,
        tools: list[str] | None = None,
        append_system_prompt: str | None = None,
        add_dirs: list[Path] | None = None,
    ) -> None:
        self._model = model
        self._workspace = workspace
        self._tools = tools
        self._append_system_prompt = append_system_prompt
        self._add_dirs = add_dirs or []
        self._session_id = str(uuid.uuid4())
        self._proc: asyncio.subprocess.Process | None = None
        self._stdin_lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    async def start(self) -> None:
        args = [
            "claude",
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", self._model,
            "--session-id", self._session_id,
        ]
        if self._tools is not None:
            args += ["--tools", ",".join(self._tools)]
            # Any tool we explicitly listed gets pre-permitted — a voice agent
            # can't sit on a permission prompt waiting for input. --tools picks
            # what's available; --allowedTools is what runs without asking.
            if self._tools:
                args += ["--allowedTools", ",".join(self._tools)]
        for d in self._add_dirs:
            args += ["--add-dir", str(d)]
        if self._append_system_prompt:
            args += ["--append-system-prompt", self._append_system_prompt]
        log.info("spawn long-running claude: %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._workspace),
            limit=STDOUT_BUFFER_LIMIT,
        )

    async def send_user(self, text: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        payload = json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}}
        )
        log.info("send_user: %r", text)
        async with self._stdin_lock:
            self._proc.stdin.write(payload.encode() + b"\n")
            await asyncio.wait_for(self._proc.stdin.drain(), STDIN_FLUSH_TIMEOUT)

    async def lines(self) -> AsyncIterator[bytes]:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            try:
                line = await self._proc.stdout.readline()
            except ValueError as e:
                # readline raises ValueError if a single line exceeds the
                # StreamReader limit. The buffer is auto-cleared; log and keep
                # going so a single oversized event doesn't kill the session.
                log.error("stdout readline overflow, dropping line: %s", e)
                continue
            if not line:
                return
            yield line

    async def stop(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception as e:
            log.debug("stdin close raised: %s", e)
        try:
            await asyncio.wait_for(proc.wait(), SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("claude didn't exit within %ss; killing", SHUTDOWN_TIMEOUT)
            proc.kill()
            await proc.wait()
