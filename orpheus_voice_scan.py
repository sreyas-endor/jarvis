"""Orpheus-3B voice scan via mlx-audio.

Generates one ~5s sample per named voice (Canopy fine-tune speakers) and saves
to /tmp/orpheus_voices/. Does NOT auto-play. Use Finder + QuickLook to listen.

Voices (per Canopy Orpheus-TTS):
    tara, leah, jess, mia, zoe  — female (likely)
    leo, dan, zac               — male (likely)
The labels aren't documented per-voice; we're going by community convention.
Ground truth = your ears.

Voice cloning is currently broken in Orpheus (mlx-audio source carries an
explicit warning). So we're stuck with the named voices for now.

Run:
    cd ~/Code/jarvis
    PYTHONUNBUFFERED=1 uv run --python 3.12 \\
      --with mlx-audio \\
      python -u orpheus_voice_scan.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ["TOKENIZERS_PARALLELISM"] = "false"

def step(msg: str) -> None:
    print(msg, flush=True)


step("[1/3] importing mlx-audio (first import is slow — pulls torch + transformers)...")
_t = time.perf_counter()
import audiofile
import mlx.core as mx
import numpy as np

from mlx_audio.tts.utils import load_model

step(f"      imports done in {time.perf_counter() - _t:.1f}s")

MODEL_REPO = "mlx-community/orpheus-3b-0.1-ft-bf16"
SAMPLE_RATE = 24000

VOICES = ["tara", "leah", "jess", "mia", "zoe", "leo", "dan", "zac"]

PROMPT = (
    "Hello, my name is Jarvis. I'm a voice assistant. "
    "I can help you with reading files and answering questions. "
    "What can I do for you today?"
)

OUT_DIR = Path("/tmp/orpheus_voices")


def load_orpheus():
    step(f"\n[2/3] loading {MODEL_REPO} (first time = ~6GB download)...")
    t0 = time.perf_counter()
    model = load_model(MODEL_REPO)
    step(f"      loaded in {time.perf_counter() - t0:.1f}s")
    return model


def generate_for_voice(model, voice: str) -> tuple[float, float, np.ndarray]:
    """Returns (ttfa_sec, total_sec, pcm_float32)."""
    chunks: list[mx.array] = []
    ttfa: float | None = None
    t0 = time.perf_counter()
    for result in model.generate(
        text=PROMPT,
        voice=voice,
        temperature=0.6,
        top_p=0.8,
        max_tokens=1500,
        stream=True,
        streaming_interval=0.5,  # smaller = lower TTFA, more frequent yields
    ):
        audio = result.audio
        if audio is None or audio.shape[0] == 0:
            continue
        mx.eval(audio)
        if ttfa is None:
            ttfa = time.perf_counter() - t0
        chunks.append(audio)
    total = time.perf_counter() - t0
    if not chunks:
        return ttfa or 0.0, total, np.zeros(0, dtype=np.float32)
    pcm = np.asarray(mx.concat(chunks)).astype(np.float32)
    return ttfa or 0.0, total, pcm


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    model = load_orpheus()

    step("\n      warmup pass (JIT, untimed)...")
    generate_for_voice(model, "tara")

    step(f"\n[3/3] generating each named voice with prompt={PROMPT!r}\n")
    for voice in VOICES:
        ttfa, total, pcm = generate_for_voice(model, voice)
        audio_secs = len(pcm) / SAMPLE_RATE
        rtf = total / audio_secs if audio_secs > 0 else float("inf")
        out_path = OUT_DIR / f"voice_{voice}.wav"
        if len(pcm) > 0:
            audiofile.write(str(out_path), pcm, SAMPLE_RATE)
        step(
            f"  voice={voice:<6}  ttfa={ttfa * 1000:>5.0f}ms  "
            f"total={total * 1000:>6.0f}ms  audio={audio_secs:>4.1f}s  "
            f"rtf={rtf:>4.2f}x  -> {out_path}"
        )

    step(f"\n=== DONE ===\nSamples in {OUT_DIR}.")
    step("Listen via: open /tmp/orpheus_voices  (Finder + space for QuickLook)")
    step("Or: afplay /tmp/orpheus_voices/voice_tara.wav")


if __name__ == "__main__":
    main()
