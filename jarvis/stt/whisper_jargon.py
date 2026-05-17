"""Whisper MLX STT with jargon biasing via initial_prompt.

Subclasses Pipecat's WhisperSTTServiceMLX and overrides run_stt to pass an
`initial_prompt` through to `mlx_whisper.transcribe`. The prompt biases
Whisper toward recognizing developer/work jargon (tool names, library
names, acronyms) that the base model otherwise mangles.

Whisper's prompt is treated by the model as recent prior context. Prose-
style phrasing outperforms a raw word list; the first ~224 tokens survive
truncation, so put the highest-value terms early.

The rest of run_stt is faithfully copied from the parent — hallucination
filter (compression_ratio==0.5555…), no_speech filter, and the
TranscriptionFrame yield — so behavior is identical except for the
biasing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import numpy as np
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.settings import assert_given
from pipecat.services.whisper.stt import WhisperSTTServiceMLX
from pipecat.utils.time import time_now_iso8601


class WhisperJargonSTTService(WhisperSTTServiceMLX):
    """Whisper MLX STT biased toward a fixed jargon vocabulary."""

    def __init__(self, *, initial_prompt: str, **kwargs):
        super().__init__(**kwargs)
        self._initial_prompt = initial_prompt

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            import mlx_whisper

            await self.start_processing_metrics()

            audio_float = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

            model_path = assert_given(self._settings.model)
            if model_path is None:
                raise ValueError("Whisper model must be specified")
            temperature = assert_given(self._settings.temperature)
            language = assert_given(self._settings.language)
            chunk = await asyncio.to_thread(
                mlx_whisper.transcribe,
                audio_float,
                path_or_hf_repo=model_path,
                temperature=temperature,
                language=language,
                initial_prompt=self._initial_prompt,
            )
            text: str | None = ""
            no_speech_prob_threshold = assert_given(self._settings.no_speech_prob)
            for segment in chunk.get("segments", []):
                if segment.get("compression_ratio", None) == 0.5555555555555556:
                    continue
                if (
                    no_speech_prob_threshold is not None
                    and segment.get("no_speech_prob", 0.0) < no_speech_prob_threshold
                ):
                    text += f"{segment.get('text', '')} "

            if len(text.strip()) == 0:
                text = None

            await self.stop_processing_metrics()

            if text:
                await self._handle_transcription(text, True, language)
                logger.debug(f"Transcription: [{text}]")
                yield TranscriptionFrame(
                    text,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                )

        except Exception as e:
            yield ErrorFrame(error=f"Unknown error occurred: {e}")
