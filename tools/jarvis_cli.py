"""Bash-callable CLI for the master Claude Code session.

The voice-master Claude Code session calls these via the Bash tool to
discover and tail other Claude Code sessions on the same Mac:

  jarvis-cli sessions list            # see what else is running
  jarvis-cli sessions attach <id>     # narrate that session's major events
  jarvis-cli sessions detach <id>     # stop narrating it

All commands talk to the running Pipecat process via localhost. Output
is plain text, structured for the model to read back to the user.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PIPECAT_BASE = "http://127.0.0.1:7860"


def _http_get(path: str) -> dict:
    req = urllib.request.Request(PIPECAT_BASE + path)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _http_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        PIPECAT_BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _format_age(mtime: float) -> str:
    delta = time.time() - mtime
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def cmd_sessions_list(args: argparse.Namespace) -> int:
    # Pass the master's own session id so claude doesn't see itself.
    exclude = os.environ.get("CLAUDE_SESSION_ID") or args.exclude or ""
    qs = f"?exclude={exclude}" if exclude else ""
    try:
        payload = _http_get("/internal/sessions" + qs)
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    sessions = payload.get("sessions", [])
    attached = set(payload.get("attached", []))
    if not sessions:
        print("No other Claude Code sessions found in the last 48 hours.")
        return 0
    print(f"{len(sessions)} session(s):")
    for s in sessions:
        marker = " [attached]" if s["id"] in attached else ""
        # Show a short id so claude has something readable to speak. The
        # full id is in the same line for follow-up commands.
        short = s["id"][:8]
        age = _format_age(s["last_modified"])
        summary = s.get("summary") or "(no user message)"
        print(
            f"  {short}  {age:>8}  {s['project']}{marker}\n"
            f"      ↳ {summary}\n"
            f"      id: {s['id']}"
        )
    return 0


def _resolve_session_id(arg: str) -> str:
    """Accept either a full UUID or an unambiguous 8-char prefix."""
    if len(arg) >= 32:
        return arg
    try:
        payload = _http_get("/internal/sessions")
    except urllib.error.URLError:
        return arg  # let the server give a clearer error
    matches = [s["id"] for s in payload.get("sessions", []) if s["id"].startswith(arg)]
    if len(matches) == 1:
        return matches[0]
    return arg


def cmd_sessions_attach(args: argparse.Namespace) -> int:
    sid = _resolve_session_id(args.session_id)
    try:
        payload = _http_post("/internal/sessions/attach", {"session_id": sid})
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    if not payload.get("ok"):
        print(f"error: {payload.get('error', 'unknown')}", file=sys.stderr)
        return 1
    print(f"Attached. Now narrating events from session {payload['session_id'][:8]} "
          f"({payload.get('project','')}). Detach with: jarvis-cli sessions detach {payload['session_id'][:8]}")
    return 0


def cmd_sessions_detach(args: argparse.Namespace) -> int:
    sid = _resolve_session_id(args.session_id)
    try:
        payload = _http_post("/internal/sessions/detach", {"session_id": sid})
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    if payload.get("ok"):
        print(f"Detached session {sid[:8]}.")
        return 0
    print(f"Session {sid[:8]} was not attached.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sessions = sub.add_parser("sessions", help="manage Claude Code session attachments")
    sessions_sub = sessions.add_subparsers(dest="action", required=True)

    p_list = sessions_sub.add_parser("list", help="list recent sessions on this Mac")
    p_list.add_argument(
        "--exclude",
        default="",
        help="hide this session id from the listing (master's own id)",
    )
    p_list.set_defaults(func=cmd_sessions_list)

    p_attach = sessions_sub.add_parser("attach", help="start narrating a session")
    p_attach.add_argument("session_id")
    p_attach.set_defaults(func=cmd_sessions_attach)

    p_detach = sessions_sub.add_parser("detach", help="stop narrating a session")
    p_detach.add_argument("session_id")
    p_detach.set_defaults(func=cmd_sessions_detach)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
