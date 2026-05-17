"""Defensive newline-delimited JSON parser for Claude Code's stream output.

Claude Code's --output-format stream-json occasionally emits non-JSON lines
(warnings, stderr fragments). Return None on any unparseable input rather
than crashing the pipeline.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def parse_line(line: bytes) -> dict | None:
    """Parse one NDJSON line. Returns None for empty or malformed input."""
    text = line.decode("utf-8", errors="replace").strip()
    if not text or not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.debug("dropping unparseable line: %.120s (%s)", text, e)
        return None
