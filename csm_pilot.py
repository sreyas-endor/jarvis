"""CSM-1B latency pilot.

Measures time-to-first-audio (TTFA), per-turn total time, and real-time factor
(RTF) on this machine, with and without 4-bit quantization. Plays the first
sample of each config so we can also judge quality by ear.

Run before committing to a full Pipecat integration — the latency story
depends heavily on chip and quantization, and csm-mlx's own README says
"nearly real-time on M2 Air" only *with* quantization.

Setup:
    cd ~/Code/jarvis
    uv add 'git+https://github.com/senstella/csm-mlx' --upgrade
    uv run python csm_pilot.py

First run downloads ~6GB of weights from HuggingFace (cached after).

Reading the numbers:
    TTFA < 500ms  great                      (Kokoro is ~150-300ms)
    TTFA < 1000ms workable
    TTFA > 1500ms painful — every turn feels laggy
    RTF  < 1.0    generation is faster than playback (steady-state OK)
    RTF  > 1.0    can't keep up with playback once buffer drains (will stutter)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


def step(msg: str) -> None:
    print(msg, flush=True)


step("[1/5] importing libraries (this can take 5-20s the first time)...")
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

SPEAKERS_TO_SCAN = [0, 1, 2, 3, 4, 5]

# Four multi-sentence prompts covering different registers so we can judge
# voice quality across the kinds of things a voice agent actually says.
PROMPTS = [
    # Casual greeting + reaction + short thought — daily-driver register
    "Hey, good to hear from you. I've been wondering when you'd get around to testing this thing. "
    "So, what do you want to dig into first? I've got opinions, but you go ahead.",

    # Technical explanation — measured pace, multi-clause, em-dashes and ellipses
    "Okay, so the way this works is — the model generates audio tokens one frame at a time, "
    "and each frame is about eighty milliseconds long. That means the latency you feel comes mostly "
    "from how many frames have to land before you hear something. Smaller chunks feel snappier, "
    "but bigger chunks tend to sound more stable. There's no free lunch, basically.",

    # Emotive / conversational — tests light reactions, varied pacing
    "Oh wow, that's actually really cool. I wasn't expecting it to sound that natural — "
    "I mean, you can tell it's a model, sure, but the rhythm feels right. "
    "Hmm, I'm curious how it handles longer thoughts though, like when you really need to explain something "
    "instead of just answering a quick question. Want to try one of those next?",

    # Honest / limitation case — tests plain, direct delivery
    "Honestly, I don't know the answer to that one off the top of my head. "
    "I'd be guessing if I tried to make something up, and that's not super useful to you. "
    "If you can point me at a file or a doc, I can read it and give you a real answer. "
    "Otherwise, my best move is to just say I'm not sure.",
]


def load_csm(*, quantize: bool) -> CSM:
    label = "quantized" if quantize else "fp16"
    step(f"\n[load:{label}] initializing CSM graph...")
    t0 = time.perf_counter()
    csm = CSM(csm_1b())

    step(f"[load:{label}] resolving weights from HuggingFace (first time = ~6GB download)...")
    t1 = time.perf_counter()
    weight = hf_hub_download(repo_id="senstella/csm-1b-mlx", filename="ckpt.safetensors")
    step(f"[load:{label}] weights ready at {weight} in {time.perf_counter() - t1:.1f}s")

    step(f"[load:{label}] loading weights into model...")
    t2 = time.perf_counter()
    csm.load_weights(weight)
    step(f"[load:{label}] weights loaded in {time.perf_counter() - t2:.1f}s")

    if quantize:
        step(f"[load:{label}] applying 4-bit quantization...")
        t3 = time.perf_counter()
        nn.quantize(csm)
        step(f"[load:{label}] quantized in {time.perf_counter() - t3:.1f}s")

    step(f"[load:{label}] total load: {time.perf_counter() - t0:.1f}s")
    return csm


def run_one(csm: CSM, text: str, sampler, speaker: int = 0) -> tuple[float, float, np.ndarray]:
    chunks: list[mx.array] = []
    ttfa: float | None = None
    t0 = time.perf_counter()
    for chunk in stream_generate(
        csm,
        text=text,
        speaker=speaker,
        context=[],
        max_audio_length_ms=30_000,
        accumulation_size=1,
        sampler=sampler,
    ):
        mx.eval(chunk)  # force lazy MLX compute so timing reflects reality
        if ttfa is None:
            ttfa = time.perf_counter() - t0
        chunks.append(chunk)
    total = time.perf_counter() - t0
    if not chunks:
        return ttfa or 0.0, total, np.zeros(0, dtype=np.float32)
    pcm = np.asarray(mx.concat(chunks)).astype(np.float32)
    return ttfa or 0.0, total, pcm


def gen_and_save(csm, text, sampler, speaker, out_path: Path) -> tuple[float, float, float]:
    ttfa, total, pcm = run_one(csm, text, sampler, speaker=speaker)
    audio_secs = len(pcm) / SAMPLE_RATE
    rtf = total / audio_secs if audio_secs > 0 else float("inf")
    audiofile.write(str(out_path), pcm, SAMPLE_RATE)
    return ttfa, total, rtf


def scan_speakers(csm: CSM, sampler) -> None:
    step("\n== Speaker scan (prompt 1, one WAV per speaker, auto-played in order) ==")
    text = PROMPTS[0]
    for spk in SPEAKERS_TO_SCAN:
        step(f"  speaker={spk}: generating...")
        out = Path(f"/tmp/csm_pilot_speaker_{spk}.wav")
        ttfa, total, rtf = gen_and_save(csm, text, sampler, spk, out)
        step(
            f"  speaker={spk}: ttfa={ttfa * 1000:.0f}ms  total={total * 1000:.0f}ms  "
            f"rtf={rtf:.2f}x  saved={out}"
        )
        step(f"  speaker={spk}: playing (hit Ctrl-C to skip this voice)...")
        subprocess.run(["afplay", str(out)], check=False)


def quality_pass(csm: CSM, sampler, speaker: int) -> None:
    step(f"\n== Quality pass (4 multi-sentence prompts, speaker={speaker}) ==")
    for i, prompt in enumerate(PROMPTS, 1):
        step(f"  [{i}] generating ({len(prompt)} chars)...")
        out = Path(f"/tmp/csm_pilot_quality_{i}.wav")
        ttfa, total, rtf = gen_and_save(csm, prompt, sampler, speaker, out)
        step(
            f"  [{i}] ttfa={ttfa * 1000:.0f}ms  total={total * 1000:.0f}ms  "
            f"rtf={rtf:.2f}x  saved={out}"
        )
        step(f"  [{i}] playing...")
        subprocess.run(["afplay", str(out)], check=False)


SPEAKER = 1  # locked in from earlier speaker scan


def quality_pass_labeled(csm: CSM, sampler, speaker: int, label: str) -> None:
    step(f"\n== Quality pass ({label}, 4 prompts, speaker={speaker}) ==")
    for i, prompt in enumerate(PROMPTS, 1):
        step(f"  [{i}] generating ({len(prompt)} chars)...")
        out = Path(f"/tmp/csm_pilot_speaker{speaker}_{label}_{i}.wav")
        ttfa, total, rtf = gen_and_save(csm, prompt, sampler, speaker, out)
        step(
            f"  [{i}] ttfa={ttfa * 1000:.0f}ms  total={total * 1000:.0f}ms  "
            f"rtf={rtf:.2f}x  saved={out}"
        )
        step(f"  [{i}] playing...")
        subprocess.run(["afplay", str(out)], check=False)


def main() -> None:
    sampler = make_sampler(temp=0.8, top_k=50)

    # First: full quant quality pass on speaker 1 across all 4 prompts.
    # Numbers from the prior run looked great (~100ms TTFA, ~0.5x RTF);
    # this re-runs to confirm all 4 prompts sound good (not just prompt 1).
    csm_q = load_csm(quantize=True)
    step("\nwarmup (untimed, JIT-compiles the graph)...")
    run_one(csm_q, "Warmup sentence to compile the graph.", sampler, speaker=SPEAKER)
    quality_pass_labeled(csm_q, sampler, SPEAKER, label="quant")
    del csm_q  # free memory before loading fp16

    # Second: fp16 same prompts so we have a direct A/B.
    # Expect ~3-5x slower TTFA but the "brilliant" quality we want.
    csm_f = load_csm(quantize=False)
    step("\nwarmup (untimed, JIT-compiles the graph)...")
    run_one(csm_f, "Warmup sentence to compile the graph.", sampler, speaker=SPEAKER)
    quality_pass_labeled(csm_f, sampler, SPEAKER, label="fp16")

    step(
        "\nDone. WAVs are at /tmp/csm_pilot_speaker1_<label>_<n>.wav"
        "\nDirect A/B: afplay /tmp/csm_pilot_speaker1_quant_2.wav   then   afplay /tmp/csm_pilot_speaker1_fp16_2.wav"
    )


if __name__ == "__main__":
    main()
