"""OpenAI TTS routed through Azure AI Foundry (gpt-4o-mini-tts).

Subclasses Pipecat's OpenAITTSService and swaps the AsyncOpenAI client for
AsyncAzureOpenAI. All TTS settings (voice, instructions, speed, sample rate)
behave identically to the base class — only the auth path and endpoint
differ. In Azure mode the `model` field is interpreted as the deployment
name, not the OpenAI model ID.

Works with Foundry-deployed gpt-4o-mini-tts, tts-hd, and tts.
"""
from __future__ import annotations

import os

from openai import AsyncAzureOpenAI
from pipecat.services.openai.tts import OpenAITTSService

from jarvis._util import require_env

# gpt-4o-mini-tts is the only OpenAI TTS model that supports `instructions`.
# tts-hd and tts ignore it but still use the voice setting.
DEFAULT_DEPLOYMENT = "gpt-4o-mini-tts"
DEFAULT_API_VERSION = "2024-10-01-preview"
# fable — explicitly described as "warm, storytelling"; closest literal
# match to Cartesia Skylar's character in the OpenAI voice set.
DEFAULT_VOICE = "fable"
DEFAULT_SPEED = 1.0
# Community findings (community.openai.com): gpt-4o-mini-tts handles ONE
# concrete persona much better than a stack of adjectives. Short single-
# persona prompts beat long descriptive ones. Edit in-place to tune
# delivery; not overridable via env (intentional — keep the prompt in
# code where it's reviewable alongside voice selection).
# N.B. There's a known regression in the current model alias — pinning
# the Foundry deployment to model version 2025-03-20 restores stronger
# instruction-following and naturalness.
DEFAULT_INSTRUCTIONS = (
    "Speak like a warm, engaged podcast host chatting with a close friend. "
    "Natural conversational pace, genuine smile in your voice."
)


class AzureOpenAITTSService(OpenAITTSService):
    """OpenAI TTS via Azure AI Foundry."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        api_version: str,
        deployment: str,
        voice: str = "nova",
        instructions: str | None = None,
        speed: float | None = None,
        **kwargs,
    ):
        settings = OpenAITTSService.Settings(
            model=deployment,
            voice=voice,
            instructions=instructions,
            speed=speed,
        )
        super().__init__(api_key=api_key, settings=settings, **kwargs)
        self._client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
        )


def build_azure_openai_tts() -> AzureOpenAITTSService:
    return AzureOpenAITTSService(
        api_key=require_env("AZURE_OPENAI_API_KEY"),
        endpoint=require_env("AZURE_OPENAI_ENDPOINT"),
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION),
        deployment=os.environ.get("AZURE_OPENAI_TTS_DEPLOYMENT", DEFAULT_DEPLOYMENT),
        voice=os.environ.get("OPENAI_TTS_VOICE", DEFAULT_VOICE),
        instructions=DEFAULT_INSTRUCTIONS,
        speed=DEFAULT_SPEED,
    )
