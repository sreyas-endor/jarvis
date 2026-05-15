# Jarvis

Voice-controlled laptop assistant. Mic in, Claude Code as the brain, voice out.
Everything runs locally — STT, TTS, and audio I/O on your machine — except for
whichever cloud providers you wire up for higher-quality speech.

```
                ┌──────────────────────────────────────────────────────┐
                │                  Pipecat pipeline                    │
                │                                                      │
  mic ─▶ VAD ─▶ │ STT ─▶ ClaudeCodeLLMService ─▶ TTS ─▶ EventLogger ─▶ │ ─▶ speaker
                │   ▲             │                                    │
                │   │             ▼                                    │
                │ jargon     `claude -p --input-format stream-json`    │
                │  bias        (long-running subprocess, NDJSON IO)    │
                └──────────────────────────────────────────────────────┘
```

The interesting bit is the **LLM service**: it talks to a persistent
[Claude Code](https://docs.claude.com/en/docs/claude-code/overview) CLI
subprocess over stdin/stdout in stream-json mode, so the assistant has
file-reading tools and full Claude Code's behaviour, not just a chat model.

---

## Why

- **Pair-program by voice.** Claude Code already runs in your terminal —
  Jarvis just gives it a microphone and AirPods.
- **No keys required to start.** The default fallback path (Whisper + Kokoro)
  is fully local, so the first run only needs a `claude` login.
- **Provider-swappable.** STT and TTS are both behind a single env var.
  Try four TTS backends and two STT backends without touching code.

---

## Architecture

### Pipeline (`main.py`)

A standard [Pipecat](https://github.com/pipecat-ai/pipecat) pipeline:

1. `LocalAudioTransport.input()` — reads the default macOS input device
2. `VADProcessor(SileroVADAnalyzer)` — emits start/stop-of-speech frames
3. `_build_stt()` — Azure Speech (phrase list) or Whisper MLX (offline)
4. `ClaudeCodeLLMService` — speaks to a persistent `claude -p` subprocess
5. `_build_tts()` — Cartesia / Azure / OpenAI / MAI-Voice-1
6. `EventLogger` — prints every meaningful frame so the run is readable
7. `LocalAudioTransport.output()` — plays TTS audio out the default device

### LLM glue (`jarvis/`)

| File | Role |
|---|---|
| `claude_code_llm_service.py` | Pipecat `LLMService` driving Claude Code. Owns barge-in (`_suppress_text_until_next_send`, `_awaiting_response`), forwards transcriptions, and pushes `InterruptionFrame` on VAD start so TTS flushes mid-sentence. |
| `claude_streaming.py` | One long-running `claude -p --input-format stream-json` subprocess. Reuses the process across turns (~1–1.5s/turn warm vs ~6s cold). 10MB stdout buffer because tool results can exceed the asyncio readline default. Generates a per-conversation session UUID inline. |
| `event_bridge.py` | Translates Claude Code's NDJSON stream events into Pipecat frames (`LLMTextFrame`, `LLMFullResponseStartFrame`, `LLMFullResponseEndFrame`) plus internal items (`ToolUseStart`, `PermissionRequest`, `TurnComplete`). |
| `ndjson_parser.py` | Defensive line-by-line JSON parser. Drops non-JSON noise rather than crashing. |

### STT services

Both STT backends read jargon terms from the same `JARGON_PHRASES` list in
`main.py` (56 dev/work terms). Adding a word benefits whichever backend you
have selected.

| File | Notes |
|---|---|
| `azure_phraselist_stt_service.py` | Subclasses `AzureSTTService`. Attaches a `PhraseListGrammar` to the recognizer at connect time — biases at the **acoustic** layer, strictly stronger than Whisper's LM prompt biasing. |
| `whisper_jargon_stt_service.py` | Subclasses `WhisperSTTServiceMLX`. Overrides `run_stt` to pass `initial_prompt` through to `mlx_whisper.transcribe`. Faithfully replicates the parent's hallucination filter and no-speech threshold. ~158/224 prompt tokens used. |

### TTS services

| Provider | File | Voice / model |
|---|---|---|
| `cartesia` | (built-in `CartesiaTTSService`) | Sonic-3 + Skylar (`db6b0ed5-d5d3-463d-ae85-518a07d3c2b4`). Best naturalness. |
| `azure` | (built-in `AzureTTSService`) | `en-US-AvaMultilingualNeural` with `style=friendly`, `style_degree=1.5`, no pitch shift. |
| `openai` | `azure_openai_tts_service.py` | `gpt-4o-mini-tts` via Azure AI Foundry. `OpenAITTSService` subclass swapping `AsyncOpenAI` → `AsyncAzureOpenAI`. Default voice `fable` + short persona-anchored instructions. |
| `mai` | (built-in `AzureTTSService`) | Microsoft MAI-Voice-1 (`en-US-June:MAI-Voice-1`). Uses the regular Azure Speech SDK — only the voice name format differs. |

### Voice persona

`workspace/CLAUDE.md` is the system prompt for the Claude Code subprocess. It
tells the model to behave as a voice assistant (no markdown, conversational
register, voice-format rules). Edit it to change how Jarvis sounds.

---

## Setup

### Requirements

- macOS with Apple Silicon (MLX models are M-series-only)
- Python 3.12 (`pyproject.toml` pins `>=3.12,<3.13`)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) installed and logged in (`claude` on `$PATH`)
- AirPods or wired headphones (laptop speakers create an echo loop with the mic)

### Install

```bash
git clone https://github.com/sreyas-endor/jarvis.git
cd jarvis
uv sync
cp .env.example .env
# Edit .env and fill in whichever providers you want to use
```

### Verify the mic

```bash
uv run python check_mic.py
```

You should see RMS go up when you talk. If it stays at 0, the issue is OS-level
(mic permission, wrong device, hardware muted) — not Pipecat.

### Run

```bash
uv run python main.py
```

Speak. Wait a beat. Listen. Talk over Jarvis to interrupt.

---

## Configuration

All knobs are environment variables. Set them in `.env` (auto-loaded) or
inline (`TTS_PROVIDER=azure uv run python main.py`).

| Var | Default | Notes |
|---|---|---|
| `TTS_PROVIDER` | `cartesia` | `cartesia` \| `azure` \| `openai` \| `mai` |
| `STT_PROVIDER` | `azure` | `azure` \| `whisper` |
| `CARTESIA_API_KEY` | — | Required for `cartesia` |
| `AZURE_SPEECH_KEY` | — | Required for `azure` STT and for `azure` / `mai` TTS |
| `AZURE_SPEECH_REGION` | — | e.g. `eastus` |
| `AZURE_SPEECH_VOICE` | `en-US-AvaMultilingualNeural` | Azure TTS only |
| `AZURE_SPEECH_STYLE` | `friendly` | Azure TTS only |
| `AZURE_SPEECH_STYLE_DEGREE` | `1.5` | Azure TTS only |
| `AZURE_SPEECH_PITCH` | unset | Azure TTS only |
| `AZURE_SPEECH_RATE` | unset | Azure TTS only |
| `MAI_VOICE` | `en-US-June:MAI-Voice-1` | MAI TTS only |
| `AZURE_OPENAI_API_KEY` | — | Required for `openai` |
| `AZURE_OPENAI_ENDPOINT` | — | Foundry resource endpoint |
| `AZURE_OPENAI_API_VERSION` | `2024-10-01-preview` | OpenAI TTS only |
| `AZURE_OPENAI_TTS_DEPLOYMENT` | `gpt-4o-mini-tts` | Foundry deployment name |
| `OPENAI_TTS_VOICE` | `fable` | OpenAI TTS only |

`OPENAI_DEFAULT_INSTRUCTIONS` and the other prompt-style defaults live in
`main.py` rather than env — intentionally, so the prompt sits next to the voice
selection in code where it's reviewable.

### Provider tradeoffs

- **Cartesia (Sonic-3 + Skylar)** — daily-driver pick. ~22 min/mo on the free
  tier; ~111 min/mo on $4 Pro.
- **Azure Speech (Ava + friendly@1.5)** — smooth, slightly less warm than
  Skylar. Same key works for the STT phrase list.
- **OpenAI gpt-4o-mini-tts** — `fable` + a short single-persona prompt ("warm,
  engaged podcast host") performs best. Long stacked-trait prompts make it
  read robotic. There's a documented regression in the current model alias;
  pinning the Foundry deployment to `2025-03-20` is the cleanest lever.
  Structural ceiling ~85% of Cartesia even perfectly tuned.
- **MAI-Voice-1** — Microsoft's flagship expressive TTS (public preview).
  $22/1M chars (~50× standard Azure Speech, but trivial for personal use).
  Six voices: Jasper / June / Grant / Iris / Reed / Joy. Only available in
  select Azure regions at preview launch; provision your Azure Speech
  resource in a supported region if you see "unrecognized voice name" 4xx.

### Adding a jargon term

Open `main.py`, append to `JARGON_PHRASES`. Both STT backends pick it up:

```python
JARGON_PHRASES = [
    "Claude", "Claude Code", "Anthropic", "Pipecat", "Cartesia",
    ...
    "your-new-term",
]
```

Highest-value terms first — Whisper's prompt truncates past ~224 tokens
(Azure has no equivalent cap).

---

## How it actually works

### Barge-in (the user interrupts Jarvis mid-sentence)

`VADProcessor` emits `VADUserStartedSpeakingFrame` as soon as Silero hears
voice. `ClaudeCodeLLMService.process_frame` catches it and:

1. Sets `_suppress_text_until_next_send = True` — drops any text frames
   Claude Code is still streaming from the *previous* turn.
2. Pushes an `InterruptionFrame` downstream — TTS flushes, the output
   transport drains queued audio.

When the transcription arrives, `_suppress_text_until_next_send` is cleared
and `_awaiting_response` is set so we hold back text frames until Claude
*starts responding to our most recent send* — robust to Claude consolidating
multiple rapid user messages into one response.

### Claude Code stream-json

`claude -p --input-format stream-json --output-format stream-json` keeps a
session open. User utterances are written as JSON lines:

```json
{"type":"user","message":{"role":"user","content":"hey jarvis"}}
```

Assistant events come back on stdout (`stream_event` → `message_start` /
`content_block_delta` / etc.). `event_bridge.py` maps them to Pipecat frames.
Tool use is logged but currently auto-denied (voice permission prompts are TBD).

### Pre-permitted tools

The subprocess is launched with `--tools Read --allowedTools Read`, so Claude
can read files in `workspace/` without ever pausing on a permission prompt.
A voice agent can't sit waiting for a Y/N. Add tools to `DEFAULT_TOOLS` in
`claude_code_llm_service.py`.

### The IPv4 monkeypatch

`api.cartesia.ai` is on CloudFront. Outbound IPv6 to CloudFront silently times
out on some networks (mine included). The `websockets` library picks the
first `getaddrinfo` result and stalls 20s on the v6 socket. `main.py` filters
v6 out of DNS for this process only:

```python
def _ipv4_only_getaddrinfo(*args, **kwargs):
    return [r for r in _real_getaddrinfo(*args, **kwargs) if r[0] == _socket.AF_INET]
_socket.getaddrinfo = _ipv4_only_getaddrinfo
```

Process-scoped. Nothing system-wide changes. If you don't hit the issue, the
patch is a no-op.

---

## Project layout

```
jarvis/
├── main.py                              # Pipeline entry + provider factories
├── check_mic.py                         # OS-level mic sanity check
├── debug_audio.py                       # Pipecat audio-input debugger
├── jarvis/                              # Python package
│   ├── claude_code_llm_service.py       # Pipecat LLMService → Claude Code
│   ├── claude_streaming.py              # Long-running `claude -p` subprocess
│   ├── event_bridge.py                  # Claude NDJSON → Pipecat frames
│   ├── ndjson_parser.py                 # Defensive line parser
│   ├── azure_phraselist_stt_service.py  # Azure STT + jargon phrase list
│   ├── whisper_jargon_stt_service.py    # Whisper MLX + initial_prompt
│   └── azure_openai_tts_service.py      # OpenAI TTS via Azure Foundry
├── workspace/
│   └── CLAUDE.md                        # Voice persona system prompt
├── csm_*.py, orpheus_voice_scan.py      # Standalone TTS research scripts (historical)
├── pyproject.toml                       # uv / hatch project config
└── uv.lock                              # Locked deps
```

---

## Contributing

### Adding a new TTS provider

1. If the provider has a Pipecat service, just add a branch to `_build_tts()`
   in `main.py` keyed on `TTS_PROVIDER`.
2. If it doesn't, write a small subclass under `jarvis/` (look at
   `azure_openai_tts_service.py` for the minimal pattern — swap a client,
   keep the parent's settings dataclass). Wire it into `_build_tts()`.
3. Add the env vars to `.env.example` and document tradeoffs in the README
   provider table.

### Adding a new STT provider

Same shape: branch in `_build_stt()`, read from `JARGON_PHRASES` if the
provider supports any form of vocabulary biasing.

### Style

- No emojis in code or commits unless asked.
- Comments explain *why*, not *what*. Behaviour the next reader couldn't
  guess (a workaround, a hidden constraint) — not paraphrases of code.
- Keep the provider switch in `_build_tts()` / `_build_stt()` flat — branches
  read top-to-bottom, no factory abstraction.

---

## Known issues / next steps

- **MAI-Voice-1 untested end-to-end.** Wired but not yet exercised. First
  failure mode to expect: region mismatch (provision Azure Speech in a
  MAI-supported region).
- **Phone-as-mic via Pipecat WebRTC.** Sketched (laptop runs STT/LLM/TTS,
  phone is a dumb browser client over WebRTC). Not implemented. ~1 day of work.
- **Voice permission prompts.** Tool permission requests are currently
  auto-denied. A spoken "Claude wants to read X — allow?" handoff is the
  intended fix.

---

## Acknowledgements

- [Pipecat](https://github.com/pipecat-ai/pipecat) — pipeline framework
- [Cartesia](https://cartesia.ai/) — Sonic-3 / Skylar
- [Whisper MLX](https://github.com/ml-explore/mlx-examples) — local STT on Apple Silicon
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) — the brain
