"""Azure Speech STT with jargon phrase list biasing.

Subclasses Pipecat's AzureSTTService and attaches a PhraseListGrammar to
the recognizer at connect time. Phrase lists bias recognition at the
acoustic-model layer — strictly more powerful than language-model prompt
biasing (e.g. Whisper's initial_prompt) for known-vocabulary cases.

Docs: https://learn.microsoft.com/azure/ai-services/speech-service/improve-accuracy-phrase-list
"""

from __future__ import annotations

from collections.abc import Sequence

from azure.cognitiveservices.speech import PhraseListGrammar
from pipecat.services.azure.stt import AzureSTTService


class AzurePhraseListSTTService(AzureSTTService):
    """Azure STT with a static jargon phrase list attached at connect time."""

    def __init__(
        self,
        *,
        phrases: Sequence[str],
        phrase_weight: float | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._phrases = list(phrases)
        self._phrase_weight = phrase_weight

    async def _connect(self):
        await super()._connect()
        if self._speech_recognizer is None:
            return
        grammar = PhraseListGrammar.from_recognizer(self._speech_recognizer)
        for phrase in self._phrases:
            grammar.addPhrase(phrase)
        if self._phrase_weight is not None:
            grammar.setWeight(self._phrase_weight)
