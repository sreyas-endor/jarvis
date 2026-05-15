"""Jarvis voice agent — mic in, Claude Code as brain, voice out.

Pipeline: LocalAudioTransport → VAD → STT → ClaudeCodeLLMService → TTS →
EventLogger → LocalAudioTransport. STT and TTS providers are swappable
via STT_PROVIDER and TTS_PROVIDER env vars; see README for the matrix.

Run with AirPods or wired headphones to avoid the laptop-speaker echo loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Outbound IPv6 to api.cartesia.ai (CloudFront) silently times out on this
# network — curl works because it does Happy Eyeballs, but the `websockets`
# library picks the first getaddrinfo result and stalls 20s on the v6 socket.
# Filter v6 out of DNS resolution for this process so every outbound socket
# uses v4. Process-scoped only; nothing system-wide changes. Must run
# BEFORE any pipecat service that opens a websocket gets imported.
import socket as _socket

_real_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    return [r for r in _real_getaddrinfo(*args, **kwargs) if r[0] == _socket.AF_INET]


_socket.getaddrinfo = _ipv4_only_getaddrinfo

# Load .env before any pipecat service tries to read env vars.
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from jarvis.pipeline import run


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run())
