"""Text-to-speech providers.

Selection is via the TTS_PROVIDER env var (default: cartesia). Add a new
provider by creating a module in this package and registering a branch in
build_tts() below. Per-provider defaults (voice IDs, instructions, deployment
names) live in each provider file alongside the build_*() factory.
"""
from __future__ import annotations

import os

from .azure import build_azure_tts
from .azure_openai import AzureOpenAITTSService, build_azure_openai_tts
from .cartesia import build_cartesia_tts
from .mai import build_mai_tts

__all__ = ["AzureOpenAITTSService", "build_tts"]


def build_tts():
    """Pick a TTS service based on TTS_PROVIDER env var (default: cartesia).

    cartesia — Sonic-3 + Skylar (best naturalness). Needs CARTESIA_API_KEY.
    azure    — Azure Speech, Ava Multilingual Neural, friendly@1.5.
    openai   — gpt-4o-mini-tts via Azure AI Foundry. Voice `fable` + persona prompt.
    mai      — Microsoft MAI-Voice-1 (preview). Uses Azure Speech credentials.
    """
    provider = os.environ.get("TTS_PROVIDER", "cartesia").lower()
    if provider == "cartesia":
        return build_cartesia_tts()
    if provider == "azure":
        return build_azure_tts()
    if provider == "openai":
        return build_azure_openai_tts()
    if provider == "mai":
        return build_mai_tts()
    raise RuntimeError(
        f"Unknown TTS_PROVIDER={provider!r}. Use 'cartesia', 'azure', 'openai', or 'mai'."
    )
