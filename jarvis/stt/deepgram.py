"""Deepgram Nova-3 STT with runtime keyterm biasing.

Nova-3's `keyterm` mechanism is in-context biasing at inference time —
fundamentally different from Azure's phrase-list re-ranking. It actually
shifts the acoustic model's hypothesis space toward the listed terms,
which is the right tool for novel/technical identifiers (file names,
file extensions, project-specific nouns). Keep the list under ~50 terms;
Deepgram docs warn accuracy degrades past that due to overfitting.

Docs: https://developers.deepgram.com/docs/keyterm
"""
from __future__ import annotations

from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transcriptions.language import Language

from jarvis._util import require_env

from .jargon import DEEPGRAM_KEYTERMS


def build_deepgram_stt() -> DeepgramSTTService:
    return DeepgramSTTService(
        api_key=require_env("DEEPGRAM_API_KEY"),
        settings=DeepgramSTTService.Settings(
            model="nova-3-general",
            language=Language.EN,
            keyterm=DEEPGRAM_KEYTERMS,
            # smart_format enables Deepgram's number/date/time formatters and
            # — critically for our use case — better handling of identifiers
            # and acronyms in the output text.
            smart_format=True,
            punctuate=True,
            interim_results=True,
        ),
    )
