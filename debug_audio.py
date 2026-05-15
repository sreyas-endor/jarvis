"""Minimal Pipecat audio-input debugger.

Strips Whisper / Claude / Kokoro out and just logs every frame passing
through. Tells us whether the mic is reaching Pipecat and whether VAD
fires when you speak.
"""

from __future__ import annotations

import asyncio
import logging

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)


class FrameLogger(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self._audio_chunks = 0
        self._max_rms = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._audio_chunks += 1
            # Compute RMS to confirm chunks actually contain mic audio
            import struct
            import math
            samples = struct.unpack(f"{len(frame.audio) // 2}h", frame.audio)
            rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
            self._max_rms = max(self._max_rms, rms)
            if self._audio_chunks % 50 == 0:
                bars = int(min(rms / 200, 40))
                print(
                    f"  [chunks={self._audio_chunks} sr={frame.sample_rate} "
                    f"bytes={len(frame.audio)} rms={rms:6.0f} max={self._max_rms:6.0f}] "
                    f"{'#' * bars}"
                )
        elif isinstance(frame, UserStartedSpeakingFrame):
            print(">>> VAD: user STARTED speaking")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            print("<<< VAD: user STOPPED speaking")
        else:
            print(f"  frame: {type(frame).__name__}")
        await self.push_frame(frame, direction)


async def main() -> None:
    logging.basicConfig(level=logging.DEBUG)

    vad = SileroVADAnalyzer(
        params=VADParams(
            confidence=0.4,
            start_secs=0.1,
            stop_secs=0.4,
            min_volume=0.0,
        )
    )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_in_channels=1,
            audio_out_enabled=False,
            vad_analyzer=vad,
        )
    )

    pipeline = Pipeline([transport.input(), FrameLogger()])
    task = PipelineTask(pipeline)
    print()
    print("=== Speak into the mic. You should see 'VAD: user STARTED speaking' lines. ===")
    print("=== If you see 'audio chunks received: N' but no VAD lines, audio is reaching ===")
    print("=== Pipecat but Silero isn't triggering. If neither shows up, the transport ===")
    print("=== isn't opening the mic. Ctrl-C to quit. ===")
    print()
    await PipelineRunner().run(task)


if __name__ == "__main__":
    asyncio.run(main())
