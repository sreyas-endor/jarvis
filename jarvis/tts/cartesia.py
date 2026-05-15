"""Cartesia TTS — Sonic-3 + Skylar voice. Daily-driver pick for naturalness."""
from __future__ import annotations

from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.transcriptions.language import Language

from jarvis._util import require_env

# "Skylar - Friendly Guide" voice ID, resolved from
# https://api.cartesia.ai/voices on 2026-05-14.
SKYLAR_VOICE_ID = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"


def build_cartesia_tts() -> CartesiaTTSService:
    return CartesiaTTSService(
        api_key=require_env("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            model="sonic-3",
            voice=SKYLAR_VOICE_ID,
            language=Language.EN,
        ),
    )
