"""Generate a single utterance in Dimitri's cloned voice and auto-play it."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import audiofile
import audresample
import mlx.core as mx
import numpy as np
from huggingface_hub import hf_hub_download
from mlx import nn
from mlx_lm.sample_utils import make_sampler

from csm_mlx import CSM, Segment, csm_1b
from csm_mlx.generation import stream_generate

SAMPLE_RATE = 24000
REFERENCE_AUDIO = Path("/tmp/clone_reference.wav")
REFERENCE_TEXT = (
    "know what to tell the tool. Now, I will tell you what I'm expecting here "
    "to happen in the next, in the upcoming months. I think that increasingly "
    "people are realizing that these tools are very central to engineering "
    "practices, and increasingly a lot of the functionality or a lot of the "
    "input that is needed in order to..."
)

TEXT = (
    "Hey team, funmaxxers, this is Dimitri here. "
    "Are you ready for Dimitri mode on your Claude?"
)

OUT_PATH = Path("/tmp/dimitri_say.wav")


def read_audio_mx(path: Path) -> mx.array:
    signal, sr = audiofile.read(str(path), always_2d=True)
    signal = audresample.resample(signal, sr, SAMPLE_RATE)
    arr = mx.array(signal)
    return arr.mean(axis=0) if arr.shape[0] >= 1 else arr.squeeze(0)


def main() -> None:
    print("loading CSM...", flush=True)
    csm = CSM(csm_1b())
    weight = hf_hub_download(
        repo_id="senstella/csm-1b-mlx", filename="ckpt.safetensors"
    )
    csm.load_weights(weight)
    nn.quantize(csm)

    print("loading reference audio...", flush=True)
    ref_audio = read_audio_mx(REFERENCE_AUDIO)
    segment = Segment(speaker=0, text=REFERENCE_TEXT, audio=ref_audio)

    sampler = make_sampler(temp=0.8, top_k=50)

    print("warmup...", flush=True)
    for _ in stream_generate(
        csm, text="hi", speaker=0, context=[segment],
        max_audio_length_ms=2_000, accumulation_size=4, sampler=sampler,
    ):
        pass

    print(f"\ngenerating: {TEXT!r}", flush=True)
    chunks: list[mx.array] = []
    t0 = time.perf_counter()
    ttfa: float | None = None
    for chunk in stream_generate(
        csm,
        text=TEXT,
        speaker=0,
        context=[segment],
        max_audio_length_ms=15_000,
        accumulation_size=4,
        sampler=sampler,
    ):
        mx.eval(chunk)
        if ttfa is None:
            ttfa = time.perf_counter() - t0
        chunks.append(chunk)
    total = time.perf_counter() - t0

    pcm = np.asarray(mx.concat(chunks)).astype(np.float32)
    audio_secs = len(pcm) / SAMPLE_RATE
    audiofile.write(str(OUT_PATH), pcm, SAMPLE_RATE)

    ttfa = ttfa or 0.0
    print(
        f"ttfa={ttfa * 1000:.0f}ms  total={total * 1000:.0f}ms  "
        f"audio={audio_secs:.1f}s  rtf={total / audio_secs:.2f}x"
    )
    print(f"saved -> {OUT_PATH}")
    print("playing...")
    subprocess.run(["afplay", str(OUT_PATH)], check=False)


if __name__ == "__main__":
    main()
