from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import websockets

from backend.settings import Settings


@dataclass(slots=True)
class TranscriptResult:
    text: str
    is_final: bool
    provider_latency_ms: int | None = None
    raw: dict[str, Any] | None = None


class DeepgramStreamingSTT:
    """Small Deepgram live-transcription client.

    Browser MediaRecorder emits WebM/Opus chunks. Deepgram can accept those
    chunks over its streaming WebSocket, then sends partial/final transcript
    messages back as JSON.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-3"
            "&smart_format=true"
            "&interim_results=true"
            "&endpointing=350"
            "&vad_events=true"
        )

    async def stream(
        self,
        audio_queue: "asyncio.Queue[bytes | None]",
        transcript_queue: "asyncio.Queue[TranscriptResult]",
        stop_event: asyncio.Event,
    ) -> None:
        if not self.settings.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set. Add it to backend/.env and restart.")

        headers = {
            "Authorization": f"Token {self.settings.deepgram_api_key}",
            "Content-Type": "audio/webm",
        }

        try:
            await self._run_socket(headers, "additional_headers", audio_queue, transcript_queue, stop_event)
        except TypeError as exc:
            # websockets 12 uses extra_headers; newer versions use additional_headers.
            if "additional_headers" not in str(exc):
                raise
            await self._run_socket(headers, "extra_headers", audio_queue, transcript_queue, stop_event)

    async def _run_socket(
        self,
        headers: dict[str, str],
        header_kwarg: str,
        audio_queue: "asyncio.Queue[bytes | None]",
        transcript_queue: "asyncio.Queue[TranscriptResult]",
        stop_event: asyncio.Event,
    ) -> None:
        kwargs = {header_kwarg: headers}
        async with websockets.connect(self.url, ping_interval=20, **kwargs) as ws:
            sender = asyncio.create_task(self._send_audio(ws, audio_queue, stop_event))
            receiver = asyncio.create_task(self._receive_transcripts(ws, transcript_queue, stop_event))
            done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()

    async def _send_audio(
        self,
        ws: Any,
        audio_queue: "asyncio.Queue[bytes | None]",
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            chunk = await audio_queue.get()
            if chunk is None:
                await ws.send(json.dumps({"type": "CloseStream"}))
                return
            await ws.send(chunk)

    async def _receive_transcripts(
        self,
        ws: Any,
        transcript_queue: "asyncio.Queue[TranscriptResult]",
        stop_event: asyncio.Event,
    ) -> None:
        async for message in ws:
            if stop_event.is_set():
                return
            data = json.loads(message)
            channel = data.get("channel") or {}
            alternatives = channel.get("alternatives") or []
            transcript = (alternatives[0].get("transcript") if alternatives else "") or ""
            transcript = transcript.strip()
            if not transcript:
                continue

            await transcript_queue.put(
                TranscriptResult(
                    text=transcript,
                    is_final=bool(data.get("is_final") or data.get("speech_final")),
                    provider_latency_ms=None,
                    raw=data,
                )
            )
