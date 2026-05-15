"""Speech-to-text providers.

Selection is via the STT_PROVIDER env var (default: deepgram). Add a new
provider by creating a module in this package and registering a branch in
build_stt() below.
"""
from __future__ import annotations

import os

from pipecat.services.whisper.stt import MLXModel, WhisperMLXSTTSettings

from jarvis._util import require_env

from .azure_phraselist import AzurePhraseListSTTService
from .deepgram import build_deepgram_stt
from .jargon import DEEPGRAM_KEYTERMS, JARGON_PHRASES, WHISPER_INITIAL_PROMPT
from .whisper_jargon import WhisperJargonSTTService

__all__ = [
    "AzurePhraseListSTTService",
    "WhisperJargonSTTService",
    "JARGON_PHRASES",
    "WHISPER_INITIAL_PROMPT",
    "DEEPGRAM_KEYTERMS",
    "build_deepgram_stt",
    "build_stt",
]


def build_stt():
    """Pick an STT service based on STT_PROVIDER env var (default: deepgram).

    deepgram — Deepgram Nova-3 with runtime keyterm biasing. Strongest
               accuracy for code-heavy / technical speech because the
               keyterm mechanism shifts the acoustic hypothesis space at
               inference time rather than re-ranking n-best. Cloud.
    azure    — Azure Speech with PhraseListGrammar attached. N-best
               re-ranking; weaker on novel identifiers. Cloud.
    whisper  — Local Whisper Large V3 Turbo Q4 with initial_prompt biasing.
               No network needed.
    """
    provider = os.environ.get("STT_PROVIDER", "deepgram").lower()
    if provider == "deepgram":
        return build_deepgram_stt()
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
        f"Unknown STT_PROVIDER={provider!r}. Use 'deepgram', 'azure', or 'whisper'."
    )
