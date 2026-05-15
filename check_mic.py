"""Mic sanity check. Records 5 seconds and prints RMS volume.

If RMS stays near zero while you're talking, the issue is at the OS level
(mic permission, wrong device, muted hardware) — not Pipecat.
"""

from __future__ import annotations

import math
import struct
import sys
import time

import pyaudio


def main() -> None:
    pa = pyaudio.PyAudio()

    print("=== devices ===")
    default_in = pa.get_default_input_device_info()
    print(f"default input: [{default_in['index']}] {default_in['name']}")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(f"  [{i}] {info['name']}  in={info['maxInputChannels']}")

    rate = 16000
    chunk = 1024
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=rate,
        input=True,
        frames_per_buffer=chunk,
    )

    print()
    print("=== speak now (5s) — watch the bar; if it's stuck at 0, mic is dead ===")
    end = time.time() + 5
    while time.time() < end:
        data = stream.read(chunk, exception_on_overflow=False)
        samples = struct.unpack(f"{len(data) // 2}h", data)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        bars = int(min(rms / 200, 60))
        sys.stdout.write(f"\rRMS {rms:6.0f}  {'#' * bars}{' ' * (60 - bars)}")
        sys.stdout.flush()
    print()

    stream.stop_stream()
    stream.close()
    pa.terminate()


if __name__ == "__main__":
    main()
