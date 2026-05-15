"""Speech-to-text providers.

Selection is via the STT_PROVIDER env var (default: azure). Add a new
provider by creating a module in this package and registering a branch in
build_stt() below.
"""
from __future__ import annotations

import os

from pipecat.services.whisper.stt import MLXModel, WhisperMLXSTTSettings

from jarvis._util import require_env

from .azure_phraselist import AzurePhraseListSTTService
from .jargon import JARGON_PHRASES, WHISPER_INITIAL_PROMPT
from .whisper_jargon import WhisperJargonSTTService

__all__ = [
    "AzurePhraseListSTTService",
    "WhisperJargonSTTService",
    "JARGON_PHRASES",
    "WHISPER_INITIAL_PROMPT",
    "build_stt",
]


def build_stt():
    """Pick an STT service based on STT_PROVIDER env var (default: azure).

    azure   — Azure Speech with PhraseListGrammar attached. Acoustic-layer
              biasing toward JARGON_PHRASES; strictly stronger than Whisper's
              LM-prompt biasing for known-vocabulary cases. Cloud.
    whisper — Local Whisper Large V3 Turbo Q4 with initial_prompt biasing.
              No network needed.
    """
    provider = os.environ.get("STT_PROVIDER", "azure").lower()
    if provider == "azure":
        return AzurePhraseListSTTService(
            api_key=require_env("AZURE_SPEECH_KEY"),
            region=require_env("AZURE_SPEECH_REGION"),
            phrases=JARGON_PHRASES,
        )
    if provider == "whisper":
        return WhisperJargonSTTService(
            initial_prompt=WHISPER_INITIAL_PROMPT,
            settings=WhisperMLXSTTSettings(model=MLXModel.LARGE_V3_TURBO_Q4.value),
        )
    raise RuntimeError(
        f"Unknown STT_PROVIDER={provider!r}. Use 'azure' or 'whisper'."
    )
