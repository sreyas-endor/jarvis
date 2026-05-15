"""Per-conversation session tracking for Claude Code's --resume flag.

A UUID is generated when the conversation starts. The first turn passes
--session-id <uuid> to create the session; subsequent turns pass --resume
<uuid> so Claude Code preserves history server-side. We never have to parse
the session id out of stream-json output.
"""

from __future__ import annotations

import uuid


class SessionManager:
    def __init__(self) -> None:
        self._session_id: str | None = None

    def get_or_create(self) -> tuple[str, bool]:
        """Return (session_id, is_resume). is_resume is False on the first call."""
        if self._session_id is None:
            self._session_id = str(uuid.uuid4())
            return self._session_id, False
        return self._session_id, True

    def reset(self) -> None:
        """Drop the current session so the next turn starts fresh."""
        self._session_id = None
