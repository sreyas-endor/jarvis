"""OpenAI TTS routed through Azure AI Foundry.

Subclasses Pipecat's OpenAITTSService and swaps the AsyncOpenAI client for
AsyncAzureOpenAI. All TTS settings (voice, instructions, speed, sample rate)
behave identically to the base class — only the auth path and endpoint
differ. In Azure-mode the `model` field is interpreted as the deployment
name, not the OpenAI model ID.

Works with Foundry-deployed gpt-4o-mini-tts, tts-hd, and tts.
"""

from __future__ import annotations

from openai import AsyncAzureOpenAI
from pipecat.services.openai.tts import OpenAITTSService


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
