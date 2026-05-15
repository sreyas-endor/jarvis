"""CSM-1B speaker-id voice scan.

Generates a short (~5s) audio sample for each speaker ID in a range,
saves them all to /tmp, and prints a summary. Does NOT auto-play — you
listen to them manually and decide which ones sound how.

Reality check (from Sesame's own README): the base model has not been
fine-tuned on any specific voice. The same speaker_id can produce
different voices on different runs. Treat this scan as one data point,
not a stable mapping. For a reliable voice, use voice cloning (reference
audio context).

Run:
    cd ~/Code/jarvis
    PYTHONUNBUFFERED=1 uv run --python 3.12 \\
      --with 'git+https://github.com/senstella/csm-mlx' \\
      python -u csm_voice_scan.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Suppress the cosmetic tokenizers-after-fork warning before HF imports.
os.environ["TOKENIZERS_PARALLELISM"] = "false"


def step(msg: str) -> None:
    print(msg, flush=True)


step("[1/3] importing libraries...")
_t = time.perf_counter()
import audiofile
import mlx.core as mx
import numpy as np
from huggingface_hub import hf_hub_download
from mlx import nn
from mlx_lm.sample_utils import make_sampler

from csm_mlx import CSM, csm_1b
from csm_mlx.generation import stream_generate

step(f"      imports done in {time.perf_counter() - _t:.1f}s")

SAMPLE_RATE = 24000

# Range to scan. Start at 0; widen if you don't find enough female voices.
# Model was officially trained with speaker=0 and speaker=1; IDs beyond
# may produce coherent voices, weird voices, or near-gibberish — that's
# part of what we're checking.
SPEAKER_RANGE = range(0, 50)

# Roughly 5 seconds when spoken naturally. Same text across all IDs so
# the only variable is the speaker.
PROMPT = (
    "Hello, my name is Jarvis. I'm a voice assistant. "
    "I can help you with reading files and answering questions. "
    "What can I do for you today?"
)

OUT_DIR = Path("/tmp/csm_voices")
MAX_AUDIO_MS = 8_000


def load_csm() -> CSM:
    step("\n[2/3] loading CSM-1B (quantized)...")
    t0 = time.perf_counter()
    csm = CSM(csm_1b())
    weight = hf_hub_download(repo_id="senstella/csm-1b-mlx", filename="ckpt.safetensors")
    csm.load_weights(weight)
    nn.quantize(csm)
    step(f"      loaded in {time.perf_counter() - t0:.1f}s")
    return csm


def generate_for_speaker(csm, sampler, speaker: int) -> tuple[float, np.ndarray]:
    chunks: list[mx.array] = []
    t0 = time.perf_counter()
    for chunk in stream_generate(
        csm,
        text=PROMPT,
        speaker=speaker,
        context=[],
        max_audio_length_ms=MAX_AUDIO_MS,
        accumulation_size=8,  # bigger chunks = faster overall, we don't need TTFA here
        sampler=sampler,
    ):
        mx.eval(chunk)
        chunks.append(chunk)
    total = time.perf_counter() - t0
    if not chunks:
        return total, np.zeros(0, dtype=np.float32)
    pcm = np.asarray(mx.concat(chunks)).astype(np.float32)
    return total, pcm


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    csm = load_csm()
    sampler = make_sampler(temp=0.8, top_k=50)

    step("\n      warmup pass (JIT)...")
    generate_for_speaker(csm, sampler, speaker=0)

    step(f"\n[3/3] scanning speakers {SPEAKER_RANGE.start}..{SPEAKER_RANGE.stop - 1}"
         f"  prompt={PROMPT!r}")

    summary: list[tuple[int, float, Path]] = []
    for spk in SPEAKER_RANGE:
        total, pcm = generate_for_speaker(csm, sampler, speaker=spk)
        audio_secs = len(pcm) / SAMPLE_RATE
        out_path = OUT_DIR / f"speaker_{spk:03d}.wav"
        audiofile.write(str(out_path), pcm, SAMPLE_RATE)
        step(f"  speaker={spk:>3}  audio={audio_secs:>4.1f}s  gen={total:>5.1f}s  -> {out_path}")
        summary.append((spk, audio_secs, out_path))

    step("\n=== DONE ===")
    step(f"All {len(summary)} samples saved under {OUT_DIR}")
    step("Listen via:")
    step(f"  open {OUT_DIR}        # Finder, sort by name, hit space for QuickLook")
    step(f"  afplay {OUT_DIR}/speaker_017.wav")
    step("")
    step("As you listen, jot down which IDs sound female / male / unclear /"
         " broken. The base model has no documented gender mapping, so this is"
         " your ground truth.")


if __name__ == "__main__":
    main()
