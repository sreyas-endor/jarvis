"""Jarvis-owned worker Claude sessions hosted in tmux.

A worker is a detached tmux session running an interactive ``claude``
process. We control input via ``tmux send-keys`` and observe output by
tailing the session's transcript JSONL file. The user can simultaneously
``tmux attach -t jarvis-<name>`` from iTerm to watch (or type) into the
same pane.

Why tmux instead of headless ``claude -p stream-json``:
  - User can see the live UI, including planner output, file diffs, and
    permission prompts the way they would on the desktop.
  - Voice and keyboard share one input surface, so dictation lands in
    the same place the user would type.
  - JSONL transcripts are unchanged regardless of TTY mode, so our
    existing tailing / narration infrastructure keeps working.

The price: we can't stream stdin/stdout the way stream-json mode does.
Injection happens through tmux's input multiplexer, which is line-based
and shell-escape sensitive. We use ``load-buffer`` + ``paste-buffer``
for the payload itself (immune to special chars) and ``send-keys Enter``
to submit, which keeps escaping simple.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Every Jarvis-spawned tmux session starts with this prefix. The
# convention is what lets ``list()`` enumerate workers cheaply without
# colliding with the user's own tmux sessions.
TMUX_PREFIX = "jarvis-"

# Where claude's per-session transcripts live. Same path computation
# claude uses internally — directory name is the cwd with /-replaced.
TRANSCRIPTS_ROOT = Path.home() / ".claude" / "projects"


def _encode_cwd_for_transcript_dir(cwd: str) -> str:
    """Mirror claude's encoding: leading '-' + slashes -> dashes."""
    return "-" + cwd.lstrip("/").replace("/", "-")


@dataclass
class Worker:
    """One running Jarvis worker."""

    name: str                       # short user-facing handle
    tmux_session: str               # actual tmux session name (jarvis-<name>)
    cwd: str
    session_id: str                 # UUID passed to claude --session-id
    transcript_path: Path
    spawned_at: float = field(default_factory=time.time)

    def is_alive(self) -> bool:
        try:
            subprocess.run(
                ["tmux", "has-session", "-t", self.tmux_session],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except subprocess.CalledProcessError:
            return False


def _run_tmux(args: list[str]) -> tuple[int, str]:
    """Run a tmux command, returning (exit_code, combined_output)."""
    res = subprocess.run(
        ["tmux", *args], capture_output=True, text=True
    )
    out = (res.stdout + res.stderr).strip()
    return res.returncode, out


def _safe_name(raw: str) -> str:
    """Reduce a free-form name to a tmux-safe token."""
    # tmux session names allow most ASCII but disallow '.' ':' and whitespace.
    # Also keep it short for readability.
    cleaned = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in raw.lower().strip()
    )
    cleaned = "-".join(filter(None, cleaned.split("-")))[:32] or "worker"
    return cleaned


class WorkerManager:
    """Spawns and tracks Jarvis-owned tmux workers."""

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        self._lock = asyncio.Lock()

    # Discovery -------------------------------------------------------------

    def _discover_existing(self) -> dict[str, Worker]:
        """List currently-running ``jarvis-*`` tmux sessions.

        Used to reconcile in-memory state after a server restart — we
        don't want to lose track of workers just because the parent
        process bounced.
        """
        rc, out = _run_tmux(
            ["list-sessions", "-F", "#{session_name}\t#{session_path}\t#{session_created}"]
        )
        if rc != 0:
            return {}
        seen: dict[str, Worker] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            tmux_session, cwd, created = parts[0], parts[1], parts[2]
            if not tmux_session.startswith(TMUX_PREFIX):
                continue
            name = tmux_session[len(TMUX_PREFIX) :]
            # We have no way to recover the session_id post-spawn from
            # tmux alone, so leave it blank — narration won't bind, but
            # send-keys still works. Best-effort.
            seen[name] = Worker(
                name=name,
                tmux_session=tmux_session,
                cwd=cwd,
                session_id="",
                transcript_path=Path(),
                spawned_at=float(created or time.time()),
            )
        return seen

    async def reconcile(self) -> None:
        """Adopt orphan jarvis-* tmux sessions into the registry."""
        async with self._lock:
            existing = self._discover_existing()
            for name, w in existing.items():
                if name not in self._workers:
                    self._workers[name] = w
            # Drop stale entries whose tmux session disappeared.
            stale = [
                n for n, w in self._workers.items()
                if w.tmux_session not in (
                    {ew.tmux_session for ew in existing.values()}
                )
            ]
            for n in stale:
                self._workers.pop(n, None)

    # Lifecycle -------------------------------------------------------------

    async def spawn(
        self,
        *,
        name: str,
        cwd: str | None = None,
        initial_prompt: str | None = None,
    ) -> Worker:
        """Start a new worker. Returns immediately; tmux session detached.

        ``initial_prompt`` is typed in right after launch via send-keys —
        a convenience so the user can say "start a worker in monorepo
        and have it fix the auth bug" in one go.
        """
        async with self._lock:
            base = _safe_name(name)
            # If the desired short name collides, suffix with a counter.
            chosen = base
            n = 1
            while chosen in self._workers:
                n += 1
                chosen = f"{base}-{n}"

            session_id = str(uuid.uuid4())
            resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
            tmux_session = f"{TMUX_PREFIX}{chosen}"

            # Detached tmux: -d. New session: new-session. Working dir:
            # -c. Command: pass the full claude invocation.
            # Worker settings, passed inline so they work regardless of
            # the worker's cwd:
            #   - editorMode=emacs: flat input, no vim modal weirdness
            #     when we paste programmatically via tmux.
            #   - PreToolUse hook: reuse the master's voice permission
            #     flow so the worker's risky tool calls (Edit/Write/Bash)
            #     ask the user over the same call instead of waiting on
            #     a TUI prompt no one sees.
            jarvis_home = os.environ.get("JARVIS_HOME", "")
            hook_cmd = (
                f"{jarvis_home}/.venv/bin/python "
                f"{jarvis_home}/tools/voice_permission_hook.py"
            )
            worker_settings = {
                "editorMode": "emacs",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
                            "hooks": [
                                {"type": "command", "command": hook_cmd}
                            ],
                        }
                    ]
                },
            }
            cmd = [
                "tmux", "new-session", "-d",
                "-s", tmux_session,
                "-c", resolved_cwd,
                "claude",
                "--session-id", session_id,
                "--settings", json.dumps(worker_settings),
                # No --print: we want the interactive UI so the user
                # can attach via tmux attach.
            ]
            log.info("spawning worker %s (cwd=%s)", chosen, resolved_cwd)
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(
                    f"tmux new-session failed: {res.stderr.strip() or res.stdout.strip()}"
                )

            transcript_dir = TRANSCRIPTS_ROOT / _encode_cwd_for_transcript_dir(resolved_cwd)
            worker = Worker(
                name=chosen,
                tmux_session=tmux_session,
                cwd=resolved_cwd,
                session_id=session_id,
                transcript_path=transcript_dir / f"{session_id}.jsonl",
            )
            self._workers[chosen] = worker

        if initial_prompt:
            # Give claude a beat to draw its TUI before pasting.
            await asyncio.sleep(0.5)
            await self.send_input(chosen, initial_prompt)
        return worker

    async def send_input(self, name: str, text: str) -> bool:
        """Inject text into a worker's pane and submit.

        The worker was started with ``editorMode: emacs`` so the input
        is flat (no modal editor). Paste payload via load-buffer +
        paste-buffer to dodge shell escaping for special characters,
        then submit with Enter.
        """
        worker = self._workers.get(name)
        if worker is None:
            return False
        if not worker.is_alive():
            log.warning("send_input: worker %s died", name)
            self._workers.pop(name, None)
            return False

        load = subprocess.run(
            ["tmux", "load-buffer", "-"],
            input=text,
            text=True,
            capture_output=True,
        )
        if load.returncode != 0:
            log.error("tmux load-buffer failed: %s", load.stderr.strip())
            return False
        for seq in (
            ["paste-buffer", "-t", worker.tmux_session],
            ["send-keys", "-t", worker.tmux_session, "Enter"],
        ):
            res = subprocess.run(["tmux", *seq], capture_output=True, text=True)
            if res.returncode != 0:
                log.error("tmux %s failed: %s", seq[0], res.stderr.strip())
                return False
        return True

    async def kill(self, name: str) -> bool:
        async with self._lock:
            worker = self._workers.pop(name, None)
        if worker is None:
            return False
        rc, _ = _run_tmux(["kill-session", "-t", worker.tmux_session])
        return rc == 0

    def get(self, name: str) -> Worker | None:
        return self._workers.get(name)

    def list(self) -> list[Worker]:
        # Hide dead ones — callers usually want only live workers.
        return [w for w in self._workers.values() if w.is_alive()]
