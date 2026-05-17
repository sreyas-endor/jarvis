"""Azure Speech TTS — Ava Multilingual Neural."""
from __future__ import annotations

import os

from pipecat.services.azure.tts import AzureTTSService

from jarvis._util import require_env

# Multilingual neural voices are noticeably more natural than the older
# Aria/Jenny/Sara line. Ava is widely regarded as Azure's most natural
# conversational female.
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"


def build_azure_tts() -> AzureTTSService:
    return AzureTTSService(
        api_key=require_env("AZURE_SPEECH_KEY"),
        region=require_env("AZURE_SPEECH_REGION"),
        settings=AzureTTSService.Settings(
            voice=os.environ.get("AZURE_SPEECH_VOICE", DEFAULT_VOICE),
            language="en-US",
            pitch=os.environ.get("AZURE_SPEECH_PITCH"),
            rate=os.environ.get("AZURE_SPEECH_RATE"),
        ),
    )
