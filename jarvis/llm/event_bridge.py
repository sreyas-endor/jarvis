"""Translate Claude Code stream-json events into Pipecat frames.

stream-json event types we handle:
  stream_event / message_start                          -> LLMFullResponseStartFrame
  stream_event / content_block_start  type=tool_use     -> ToolUseStart
  stream_event / content_block_delta  type=text_delta   -> LLMTextFrame
  control_request                                       -> PermissionRequest
  result                                                -> LLMFullResponseEndFrame, TurnComplete
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator, Union

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)

log = logging.getLogger(__name__)


@dataclass
class ToolUseStart:
    name: str
    tool_use_id: str


@dataclass
class PermissionRequest:
    request_id: str
    tool: str
    args: dict


@dataclass
class TurnComplete:
    stop_reason: str | None


BridgedItem = Union[Frame, ToolUseStart, PermissionRequest, TurnComplete]


async def events_to_frames(
    events: AsyncIterator[dict],
) -> AsyncIterator[BridgedItem]:
    started = False
    async for ev in events:
        ev_type = ev.get("type")

        if ev_type == "stream_event":
            inner = ev.get("event") or {}
            inner_type = inner.get("type")

            if inner_type == "message_start" and not started:
                started = True
                yield LLMFullResponseStartFrame()

            elif inner_type == "content_block_start":
                block = inner.get("content_block") or {}
                if block.get("type") == "tool_use":
                    yield ToolUseStart(
                        name=block.get("name", ""),
                        tool_use_id=block.get("id", ""),
                    )

            elif inner_type == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield LLMTextFrame(text)

        elif ev_type == "control_request":
            data = ev.get("data") or {}
            yield PermissionRequest(
                request_id=ev.get("request_id", ""),
                tool=data.get("tool", ""),
                args=data.get("args", {}),
            )

        elif ev_type == "result":
            if started:
                yield LLMFullResponseEndFrame()
            yield TurnComplete(stop_reason=ev.get("stop_reason"))
            # Don't return — long-running stream-json mode keeps emitting
            # turns; reset per-turn state and wait for the next message_start.
            started = False
