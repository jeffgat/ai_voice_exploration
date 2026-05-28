from __future__ import annotations

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from backend.settings import get_settings
from backend.voice_session import InMemorySessionStore, VoiceSession


settings = get_settings()
store = InMemorySessionStore()

app = FastAPI(
    title="Realtime Voice Agent PoC",
    description="Browser microphone to STT to LLM orchestration to TTS demo.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_origin,
        f"http://127.0.0.1:{settings.frontend_port}",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket) -> None:
    session = VoiceSession(websocket, settings, store)
    await session.run()


@app.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    """Placeholder Twilio webhook.

    A public HTTPS URL is required for real Twilio calls. This simply documents
    the TwiML shape used by Twilio Media Streams.
    """

    host = request.headers.get("host", f"localhost:{settings.backend_port}")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/ws/twilio-media" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/twilio-media")
async def twilio_media_placeholder(websocket: WebSocket) -> None:
    """Skeleton for future bidirectional Twilio Media Streams support.

    Twilio sends JSON frames containing call lifecycle and base64-encoded raw
    audio. A production adapter would decode those frames, feed the audio into
    the same STT/orchestration/TTS pipeline, then send audio frames back.
    """

    await websocket.accept()
    await websocket.send_json(
        {
            "type": "twilio_placeholder",
            "message": "Twilio Media Streams adapter is documented but not implemented in local mode.",
        }
    )
    await websocket.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=settings.backend_port, reload=True)

