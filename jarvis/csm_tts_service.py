"""CSM-1B TTS service for Pipecat.

Wraps senstella/csm-1b-mlx with 4-bit quantization and speaker=1. Pilot
measured TTFA 58-242ms and RTF 0.5x on this machine — see
csm_pilot.py for the empirical run that locked these choices.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import audiofile
import audresample
import mlx.core as mx
import numpy as np
from huggingface_hub import hf_hub_download
from loguru import logger
from mlx import nn
from mlx_lm.sample_utils import make_sampler

from csm_mlx import CSM, Segment, csm_1b
from csm_mlx.generation import stream_generate
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

CSM_REPO = "senstella/csm-1b-mlx"
CSM_WEIGHTS = "ckpt.safetensors"
CSM_SAMPLE_RATE = 24_000
SPEAKER = 1
# Larger accumulation gives the resampler chunks the audio output queue can
# handle smoothly; accumulation_size=1 produces 80ms slivers and we observed
# mid-sentence stutter as the pipeline tried to play them. 4 → ~320ms chunks,
# adds ~80ms TTFA but eliminates the chop. Matches csm_clone_voice.py.
ACCUMULATION_SIZE = 4
MAX_AUDIO_LENGTH_MS = 30_000
# Tighter than the pilot's 0.8/50 — context-anchored generation needs lower
# temperature so the model actually adheres to the reference voice instead
# of drifting on its own. Matches csm_clone_voice.py v2.
SAMPLER_TEMP = 0.6
SAMPLER_TOP_K = 20

REFERENCE_WAV = Path(__file__).parent.parent / "assets" / "voice_reference.wav"
# Transcript of REFERENCE_WAV — must match the audio verbatim. This is
# PROMPTS[1] from csm_pilot.py (the technical-explanation prompt) which is
# what /tmp/csm_pilot_speaker1_quant_2.wav was generated from.
REFERENCE_TEXT = (
    "Okay, so the way this works is — the model generates audio tokens one frame "
    "at a time, and each frame is about eighty milliseconds long. That means the "
    "latency you feel comes mostly from how many frames have to land before you "
    "hear something. Smaller chunks feel snappier, but bigger chunks tend to "
    "sound more stable. There's no free lunch, basically."
)


def _load_reference_audio(path: Path) -> mx.array:
    signal, sr = audiofile.read(str(path), always_2d=True)
    signal = audresample.resample(signal, sr, CSM_SAMPLE_RATE)
    arr = mx.array(signal)
    if arr.shape[0] >= 1:
        arr = arr.mean(axis=0)
    else:
        arr = arr.squeeze(0)
    return arr


@dataclass
class CsmTtsSettings(TTSSettings):
    pass


class CsmTtsService(TTSService):
    """Local CSM-1B TTS, 4-bit quantized, speaker=1."""

    Settings = CsmTtsSettings
    _settings: Settings

    def __init__(self, **kwargs):
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=self.Settings(model=None, voice=None, language=None),
            **kwargs,
        )

        logger.info(f"{self}: loading CSM-1B (quant, speaker={SPEAKER})...")
        csm = CSM(csm_1b())
        weight_path = hf_hub_download(repo_id=CSM_REPO, filename=CSM_WEIGHTS)
        csm.load_weights(weight_path)
        nn.quantize(csm)
        self._csm = csm
        self._sampler = make_sampler(temp=SAMPLER_TEMP, top_k=SAMPLER_TOP_K)
        self._resampler = create_stream_resampler()

        if not REFERENCE_WAV.exists():
            raise FileNotFoundError(
                f"CSM voice reference missing at {REFERENCE_WAV}. "
                "Without it, CSM picks a random voice every turn."
            )
        logger.info(f"{self}: loading voice reference from {REFERENCE_WAV.name}")
        ref_audio = _load_reference_audio(REFERENCE_WAV)
        self._reference_segment = Segment(
            speaker=SPEAKER, text=REFERENCE_TEXT, audio=ref_audio
        )
        # Audio from prior sentences in the *current* LLM turn. Reset at turn
        # start so each new turn re-anchors only on the reference WAV; appended
        # to within a turn so each sentence carries the just-spoken sentence as
        # prosodic context (prevents voice drift across sentences).
        self._turn_segments: list[Segment] = []
        logger.info(f"{self}: CSM ready (sample_rate={CSM_SAMPLE_RATE})")

    def can_generate_metrics(self) -> bool:
        return True

    async def on_turn_context_created(self, context_id: str) -> None:
        self._turn_segments = []

    def _build_context(self) -> list[Segment]:
        return [self._reference_segment, *self._turn_segments]

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        logger.debug(
            f"{self}: generating TTS [{text}] "
            f"(turn_context={len(self._turn_segments)} prior segments)"
        )
        try:
            await self.start_tts_usage_metrics(text)

            first_chunk = True
            generated_chunks: list[mx.array] = []
            for chunk in stream_generate(
                self._csm,
                text=text,
                speaker=SPEAKER,
                context=self._build_context(),
                max_audio_length_ms=MAX_AUDIO_LENGTH_MS,
                accumulation_size=ACCUMULATION_SIZE,
                sampler=self._sampler,
            ):
                mx.eval(chunk)
                generated_chunks.append(chunk)
                samples = np.asarray(chunk).astype(np.float32)
                audio_int16 = (samples * 32767).astype(np.int16).tobytes()
                audio_data = await self._resampler.resample(
                    audio_int16, CSM_SAMPLE_RATE, self.sample_rate
                )

                if first_chunk:
                    await self.stop_ttfb_metrics()
                    first_chunk = False

                yield TTSAudioRawFrame(
                    audio=audio_data,
                    sample_rate=self.sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
                # Yield to event loop so an InterruptionFrame can cancel this
                # coroutine between chunks; without this, the sync for-loop
                # would block barge-in for the full generation.
                await asyncio.sleep(0)

            # Loop completed (no exception, no cancellation). Carry this
            # sentence's audio into the turn's context so the next sentence
            # anchors on what we just said, not just the reference WAV.
            if generated_chunks:
                self._turn_segments.append(
                    Segment(
                        speaker=SPEAKER,
                        text=text,
                        audio=mx.concat(generated_chunks),
                    )
                )
        except Exception as e:
            logger.exception(f"{self}: CSM TTS failed")
            yield ErrorFrame(error=f"CSM TTS error: {e}")
        finally:
            await self.stop_ttfb_metrics()
