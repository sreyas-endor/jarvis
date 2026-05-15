"""Azure Speech TTS — Ava Multilingual Neural with friendly style."""
from __future__ import annotations

import os

from pipecat.services.azure.tts import AzureTTSService

from jarvis._util import require_env

# Multilingual neural voices are noticeably more natural than the older
# Aria/Jenny/Sara line. Ava is widely regarded as Azure's most natural
# conversational female.
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"
# Tuned to approximate Cartesia Skylar's character: warm/approachable
# customer-care female. We push style intensity but keep pitch at default —
# Ava's natural range is already on the higher side; lifting it reads
# squeaky.
#   style="friendly"      — warmer than "chat" (casual) or "customerservice" (polished)
#   style_degree=1.5      — push expressivity (1.0=neutral, 2.0=max)
#   pitch=None            — no prosody pitch shift; override via AZURE_SPEECH_PITCH
DEFAULT_STYLE = "friendly"
DEFAULT_STYLE_DEGREE = "1.5"
DEFAULT_PITCH: str | None = None


def build_azure_tts() -> AzureTTSService:
    return AzureTTSService(
        api_key=require_env("AZURE_SPEECH_KEY"),
        region=require_env("AZURE_SPEECH_REGION"),
        settings=AzureTTSService.Settings(
            voice=os.environ.get("AZURE_SPEECH_VOICE", DEFAULT_VOICE),
            language="en-US",
            style=os.environ.get("AZURE_SPEECH_STYLE", DEFAULT_STYLE),
            style_degree=os.environ.get("AZURE_SPEECH_STYLE_DEGREE", DEFAULT_STYLE_DEGREE),
            pitch=os.environ.get("AZURE_SPEECH_PITCH", DEFAULT_PITCH),
            rate=os.environ.get("AZURE_SPEECH_RATE"),
        ),
    )
