from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx

from backend.settings import Settings


class ElevenLabsTTS:
    """ElevenLabs streaming TTS client.

    The session currently buffers the generated MP3 before sending it to the
    browser. The public API here is an async chunk iterator so true chunked
    playback can replace the buffer later without changing orchestration code.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream_speech(self, text: str) -> AsyncIterator[bytes]:
        if not self.settings.elevenlabs_api_key or not self.settings.elevenlabs_voice_id:
            raise RuntimeError(
                "ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required for TTS."
            )

        url = (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            f"{self.settings.elevenlabs_voice_id}/stream"
        )
        params = {"output_format": "mp3_44100_128", "optimize_streaming_latency": "3"}
        payload = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
        }
        headers = {
            "xi-api-key": self.settings.elevenlabs_api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }

        async for chunk in self._post_stream_with_retries(url, params, payload, headers):
            yield chunk

    async def synthesize_to_buffer(self, text: str) -> bytes:
        chunks: list[bytes] = []
        async for chunk in self.stream_speech(text):
            chunks.append(chunk)
        return b"".join(chunks)

    async def _post_stream_with_retries(
        self,
        url: str,
        params: dict[str, str],
        payload: dict,
        headers: dict[str, str],
        max_attempts: int = 3,
    ) -> AsyncIterator[bytes]:
        delay = 0.4
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    async with client.stream(
                        "POST",
                        url,
                        params=params,
                        json=payload,
                        headers=headers,
                    ) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                yield chunk
                        return
            except Exception as exc:  # provider errors are surfaced to UI by caller
                last_error = exc
                if attempt == max_attempts:
                    break
                await asyncio.sleep(delay)
                delay *= 2

        assert last_error is not None
        raise last_error

