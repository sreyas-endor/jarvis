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


def cmd_worker_spawn(args: argparse.Namespace) -> int:
    body: dict = {"name": args.name}
    if args.cwd:
        body["cwd"] = args.cwd
    if args.prompt:
        body["prompt"] = args.prompt
    try:
        payload = _http_post("/internal/worker/spawn", body)
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    if not payload.get("ok"):
        print(f"error: {payload.get('error', 'unknown')}", file=sys.stderr)
        return 1
    print(
        f"Spawned worker '{payload['name']}' in {payload['cwd']}.\n"
        f"  attach with: {payload['attach_cmd']}"
    )
    return 0


def cmd_worker_list(_args: argparse.Namespace) -> int:
    try:
        payload = _http_get("/internal/worker/list")
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    workers = payload.get("workers", [])
    focused = payload.get("focused")
    if not workers:
        print("No Jarvis workers running.")
        return 0
    print(f"{len(workers)} worker(s):")
    for w in workers:
        marker = " [focused]" if w["name"] == focused else ""
        live = "alive" if w["alive"] else "dead"
        print(f"  {w['name']:<20} {live:<5}  {w['cwd']}{marker}")
        print(f"      attach: {w['attach_cmd']}")
    return 0


def cmd_worker_send(args: argparse.Namespace) -> int:
    try:
        payload = _http_post(
            "/internal/worker/send", {"name": args.name, "text": args.text}
        )
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    if payload.get("ok"):
        print(f"Sent to {args.name}.")
        return 0
    print(f"error: send failed (worker not found or dead)", file=sys.stderr)
    return 1


def cmd_worker_kill(args: argparse.Namespace) -> int:
    try:
        payload = _http_post("/internal/worker/kill", {"name": args.name})
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    if payload.get("ok"):
        print(f"Killed worker {args.name}.")
        return 0
    print(f"error: kill failed", file=sys.stderr)
    return 1


def cmd_worker_focus(args: argparse.Namespace) -> int:
    try:
        payload = _http_post(
            "/internal/worker/focus", {"name": args.name or ""}
        )
    except urllib.error.URLError as e:
        print(f"error: pipecat unreachable ({e.reason})", file=sys.stderr)
        return 1
    if not payload.get("ok"):
        print(f"error: {payload.get('error', 'unknown')}", file=sys.stderr)
        return 1
    focused = payload.get("focused")
    if focused:
        print(
            f"Focused on '{focused}'. The user's voice now goes to that "
            f"worker until they say 'hey jarvis' or 'unfocus'."
        )
    else:
        print("Unfocused. Voice is back with the master.")
    return 0


def cmd_worker_unfocus(_args: argparse.Namespace) -> int:
    return cmd_worker_focus(argparse.Namespace(name=""))


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

    worker = sub.add_parser(
        "worker", help="spawn and control Jarvis-owned Claude workers in tmux"
    )
    worker_sub = worker.add_subparsers(dest="action", required=True)

    w_spawn = worker_sub.add_parser(
        "spawn",
        help="start a new worker. user can attach via 'tmux attach -t jarvis-<name>'",
    )
    w_spawn.add_argument("name", help="short handle (jarvis-<name> becomes the tmux session)")
    w_spawn.add_argument(
        "--cwd",
        default="",
        help="working directory the worker runs in (default: current dir)",
    )
    w_spawn.add_argument(
        "--prompt",
        default="",
        help="optional initial prompt typed into the worker on launch",
    )
    w_spawn.set_defaults(func=cmd_worker_spawn)

    w_list = worker_sub.add_parser("list", help="list Jarvis-owned workers")
    w_list.set_defaults(func=cmd_worker_list)

    w_send = worker_sub.add_parser("send", help="inject one message into a worker")
    w_send.add_argument("name")
    w_send.add_argument("text")
    w_send.set_defaults(func=cmd_worker_send)

    w_kill = worker_sub.add_parser("kill", help="terminate a worker")
    w_kill.add_argument("name")
    w_kill.set_defaults(func=cmd_worker_kill)

    w_focus = worker_sub.add_parser(
        "focus", help="route the user's voice to a worker instead of the master"
    )
    w_focus.add_argument("name")
    w_focus.set_defaults(func=cmd_worker_focus)

    w_unfocus = worker_sub.add_parser(
        "unfocus", help="pull voice routing back to the master"
    )
    w_unfocus.set_defaults(func=cmd_worker_unfocus)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
