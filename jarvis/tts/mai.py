"""MAI-Voice-1 — Microsoft's flagship expressive TTS (Aug 2025).

Uses the standard Azure Speech SDK/REST API (same key + region as
AZURE_SPEECH_KEY / AZURE_SPEECH_REGION). Voice name format is
"{base-voice}:MAI-Voice-1". Roster:
  en-US-Jasper:MAI-Voice-1   (male, expressive)
  en-US-June:MAI-Voice-1     (female, warm-conversational) ← default
  en-US-Grant:MAI-Voice-1    (male)
  en-US-Iris:MAI-Voice-1     (female)
  en-US-Reed:MAI-Voice-1     (male)
  en-US-Joy:MAI-Voice-1      (female, upbeat)
Override via MAI_VOICE env var.

Caveats:
  - Public preview; only available in select Azure regions
  - $22/1M chars (~50x standard Azure Speech, but cheap for personal use)
  - No SSML style override — MAI voices are natively expressive
"""
from __future__ import annotations

import os

from pipecat.services.azure.tts import AzureTTSService

from jarvis._util import require_env

DEFAULT_VOICE = "en-US-June:MAI-Voice-1"


def build_mai_tts() -> AzureTTSService:
    return AzureTTSService(
        api_key=require_env("AZURE_SPEECH_KEY"),
        region=require_env("AZURE_SPEECH_REGION"),
        settings=AzureTTSService.Settings(
            voice=os.environ.get("MAI_VOICE", DEFAULT_VOICE),
            language="en-US",
        ),
    )
