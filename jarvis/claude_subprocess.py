"""Spawn `claude` CLI as an async subprocess for one conversation turn.

Per-turn pattern: each user utterance launches a fresh `claude -p` call
with --output-format stream-json. Session continuity is handled by passing
--session-id on the first turn and --resume on subsequent turns.

CLI flag set is taken from rchern/pi-claude-cli (TypeScript reference).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


async def spawn_claude_turn(
    *,
    prompt: str,
    session_id: str,
    resume: bool,
    model: str,
    workspace: Path,
    tools: list[str] | None = None,
    permission_prompt_tool: str | None = None,
    append_system_prompt: str | None = None,
) -> asyncio.subprocess.Process:
    """Launch one `claude -p` turn; caller drives stdout."""
    args = [
        "claude",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", model,
    ]
    if resume:
        args += ["--resume", session_id]
    else:
        args += ["--session-id", session_id]

    # None -> omit flag (Claude's default = all tools).
    # [] -> --tools "" (disable all tools).
    # Non-empty list -> comma-joined tool names.
    if tools is not None:
        args += ["--tools", ",".join(tools)]
    if permission_prompt_tool:
        args += ["--permission-prompt-tool", permission_prompt_tool]
    if append_system_prompt:
        args += ["--append-system-prompt", append_system_prompt]

    log.info("spawn: %s", " ".join(args))
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workspace),
    )


def kill_claude(proc: asyncio.subprocess.Process) -> None:
    """Force-kill if still running. Safe to call repeatedly."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
