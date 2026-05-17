"""Claude Code PreToolUse hook → voice permission via Pipecat.

Claude Code invokes this hook synchronously before any tool call we've
matched on (see workspace/.claude/settings.local.json). The hook reads the
event JSON from stdin, asks the running Pipecat server for a voice
yes/no, and exits 0 (allow) or 2 (deny). Exit 2's stderr message is
surfaced to claude as the deny reason.

We deliberately fail closed: if Pipecat isn't running (no active call),
or the user doesn't reply within the timeout, the call is denied. Better
than auto-allowing destructive actions when no human is on the line.

Hard-blocklist patterns are checked before the voice prompt so common
disasters (rm -rf /, fork bomb, dd-to-disk) never even reach a yes/no.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

PIPECAT_URL = "http://127.0.0.1:7860/internal/permission"
# Slightly longer than the server-side voice timeout so the server gets a
# chance to deny gracefully before we time out and deny ourselves.
HTTP_TIMEOUT_SECONDS = 60

# Patterns we refuse without prompting. Each entry is matched as a Python
# regex against the full Bash command string. Hit list intentionally
# small and conservative — anything else surfaces as a voice prompt the
# user can still approve.
HARD_DENY_PATTERNS: list[str] = [
    r"\brm\s+-rf?\s+(/|~|\$HOME\b)",
    r"\bsudo\s+rm\b",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork bomb
    r"\bmkfs\b",
    r"\bdd\s+if=.+\s+of=/dev/",
    r"\b(curl|wget)\s+[^|]*\|\s*(bash|sh)\b",  # pipe-to-shell from network
]


def _hard_deny(tool: str, args: dict) -> str | None:
    if tool != "Bash":
        return None
    cmd = (args.get("command") or "").strip()
    for pat in HARD_DENY_PATTERNS:
        if re.search(pat, cmd):
            return f"static blocklist match: {pat}"
    return None


def main() -> None:
    try:
        ev = json.load(sys.stdin)
    except Exception as e:
        print(f"voice_permission_hook: bad stdin JSON: {e}", file=sys.stderr)
        sys.exit(2)

    tool = ev.get("tool_name", "") or ""
    args = ev.get("tool_input") or {}

    hard = _hard_deny(tool, args)
    if hard is not None:
        print(f"refused: {hard}", file=sys.stderr)
        sys.exit(2)

    body = json.dumps({"tool": tool, "args": args}).encode("utf-8")
    req = urllib.request.Request(
        PIPECAT_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        print(
            f"voice_permission_hook: server HTTP {e.code} -> denying",
            file=sys.stderr,
        )
        sys.exit(2)
    except urllib.error.URLError as e:
        print(
            f"voice_permission_hook: server unreachable ({e.reason}) -> denying",
            file=sys.stderr,
        )
        sys.exit(2)
    except Exception as e:
        print(f"voice_permission_hook: unexpected error {e}", file=sys.stderr)
        sys.exit(2)

    if data.get("allow") is True:
        sys.exit(0)

    reason = data.get("reason") or "user denied via voice"
    print(reason, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
