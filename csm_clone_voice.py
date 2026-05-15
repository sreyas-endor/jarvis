"""CSM-1B with Dimitri's voice cloned from /tmp/clone_reference.wav.

Generates the same 4 long prompts as the earlier quality pass, but with the
reference audio + transcript passed as Segment context so CSM mimics Dimitri's
voice instead of producing a random voice.

Outputs land in /tmp/csm_dimitri/. Listen to compare against the earlier
/tmp/csm_pilot_quality_*.wav files (those were random voice).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# All weights are cached locally — don't hit HF (network has IPv6 issues).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def step(msg: str) -> None:
    print(msg, flush=True)


step("[1/4] importing libraries...")
_t = time.perf_counter()
import audiofile
import audresample
import mlx.core as mx
import numpy as np
from huggingface_hub import hf_hub_download
from mlx import nn
from mlx_lm.sample_utils import make_sampler

from csm_mlx import CSM, Segment, csm_1b
from csm_mlx.generation import stream_generate

step(f"      imports done in {time.perf_counter() - _t:.1f}s")

SAMPLE_RATE = 24000
REFERENCE_AUDIO = Path("/tmp/clone_reference.wav")
REFERENCE_TEXT = (
    "know what to tell the tool. Now, I will tell you what I'm expecting here "
    "to happen in the next, in the upcoming months. I think that increasingly "
    "people are realizing that these tools are very central to engineering "
    "practices, and increasingly a lot of the functionality or a lot of the "
    "input that is needed in order to..."
)

PROMPTS = [
    "Hey, good to hear from you. I've been wondering when you'd get around to "
    "testing this thing. So, what do you want to dig into first? I've got "
    "opinions, but you go ahead.",

    "Okay, so the way this works is — the model generates audio tokens one "
    "frame at a time, and each frame is about eighty milliseconds long. That "
    "means the latency you feel comes mostly from how many frames have to land "
    "before you hear something. Smaller chunks feel snappier, but bigger "
    "chunks tend to sound more stable. There's no free lunch, basically.",

    "Oh wow, that's actually really cool. I wasn't expecting it to sound that "
    "natural — I mean, you can tell it's a model, sure, but the rhythm feels "
    "right. Hmm, I'm curious how it handles longer thoughts though, like when "
    "you really need to explain something instead of just answering a quick "
    "question. Want to try one of those next?",

    "Honestly, I don't know the answer to that one off the top of my head. "
    "I'd be guessing if I tried to make something up, and that's not super "
    "useful to you. If you can point me at a file or a doc, I can read it and "
    "give you a real answer. Otherwise, my best move is to just say I'm not sure.",
]

OUT_DIR = Path("/tmp/csm_dimitri_v2")


def read_audio_mx(path: Path) -> mx.array:
    signal, sr = audiofile.read(str(path), always_2d=True)
    signal = audresample.resample(signal, sr, SAMPLE_RATE)
    arr = mx.array(signal)
    if arr.shape[0] >= 1:
        arr = arr.mean(axis=0)
    else:
        arr = arr.squeeze(0)
    return arr


def load_csm() -> CSM:
    step("\n[2/4] loading CSM-1B (quantized, offline)...")
    t0 = time.perf_counter()
    csm = CSM(csm_1b())
    weight = hf_hub_download(
        repo_id="senstella/csm-1b-mlx", filename="ckpt.safetensors"
    )
    csm.load_weights(weight)
    nn.quantize(csm)
    step(f"      loaded in {time.perf_counter() - t0:.1f}s")
    return csm


def generate_with_clone(csm: CSM, sampler, context: list[Segment], text: str):
    chunks: list[mx.array] = []
    ttfa: float | None = None
    t0 = time.perf_counter()
    for chunk in stream_generate(
        csm,
        text=text,
        speaker=0,
        context=context,
        max_audio_length_ms=30_000,
        accumulation_size=4,
        sampler=sampler,
    ):
        mx.eval(chunk)
        if ttfa is None:
            ttfa = time.perf_counter() - t0
        chunks.append(chunk)
    total = time.perf_counter() - t0
    if not chunks:
        return ttfa or 0.0, total, np.zeros(0, dtype=np.float32)
    pcm = np.asarray(mx.concat(chunks)).astype(np.float32)
    return ttfa or 0.0, total, pcm


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    csm = load_csm()
    # v2: lower temp / tighter top_k so the model sticks closer to the reference
    # voice. Cost: slightly less prosodic variation. Goal: 4/4 voice fidelity.
    sampler = make_sampler(temp=0.6, top_k=20)

    step(f"\n[3/4] loading reference audio: {REFERENCE_AUDIO}")
    ref_audio = read_audio_mx(REFERENCE_AUDIO)
    duration_s = ref_audio.shape[0] / SAMPLE_RATE
    step(f"      {ref_audio.shape[0]} samples ({duration_s:.1f}s) at {SAMPLE_RATE}Hz")
    segment = Segment(speaker=0, text=REFERENCE_TEXT, audio=ref_audio)
    context = [segment]

    step("\n      warmup (untimed)...")
    generate_with_clone(csm, sampler, context, "Quick warmup line.")

    step(f"\n[4/4] generating {len(PROMPTS)} prompts with Dimitri's voice clone\n")
    for i, prompt in enumerate(PROMPTS, 1):
        step(f"  [{i}] generating ({len(prompt)} chars)...")
        ttfa, total, pcm = generate_with_clone(csm, sampler, context, prompt)
        audio_secs = len(pcm) / SAMPLE_RATE
        rtf = total / audio_secs if audio_secs > 0 else float("inf")
        out_path = OUT_DIR / f"clone_{i}.wav"
        if len(pcm) > 0:
            audiofile.write(str(out_path), pcm, SAMPLE_RATE)
        step(
            f"  [{i}] ttfa={ttfa * 1000:>5.0f}ms  total={total * 1000:>6.0f}ms  "
            f"audio={audio_secs:>4.1f}s  rtf={rtf:>4.2f}x  -> {out_path}"
        )

    step(f"\n=== DONE ===\nSamples in {OUT_DIR}.")
    step("Listen via: open /tmp/csm_dimitri")


if __name__ == "__main__":
    main()
