# Realtime Voice Agent PoC

Local developer proof-of-concept for a realtime voice agent architecture:

```text
Browser microphone
  -> FastAPI WebSocket /ws/voice
  -> Deepgram streaming STT
  -> LLM orchestration layer
  -> ElevenLabs streaming TTS client
  -> browser audio playback queue
  -> in-memory session/event log
```

The demo simulates a safe, clearly demo-only assistant discussing a fake past-due bill and possible payment arrangement. It does not process real payments, does not collect card details, and does not claim to be a real debt collector.

## Project Layout

```text
backend/
  main.py
  settings.py
  voice_session.py
  stt/deepgram.py
  tts/elevenlabs.py
  llm/orchestrator.py
  tools/account_tools.py
  guardrails/policy.py
  integrations/twilio_adapter.py
  integrations/retell_adapter.py
frontend/
  Vite + React + TypeScript app
```

## Backend Setup

```bash
cd /Users/jeffreygatbonton/Desktop/Code/voice_exploration
python3.11 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

Fill in `backend/.env`. The LLM defaults to OpenRouter with a cheap Qwen model:

```bash
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
OPENROUTER_API_KEY=...
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=qwen/qwen-2.5-7b-instruct
BACKEND_PORT=8000
FRONTEND_PORT=5173
```

`OPENAI_API_KEY` is still supported as a fallback if you point `OPENAI_BASE_URL` at another OpenAI-compatible API.

## Run Both Servers

After backend and frontend dependencies are installed, run both local dev servers from the repo root:

```bash
cd /Users/jeffreygatbonton/Desktop/Code/voice_exploration
npm run dev
```

That starts:

- backend on [http://localhost:8000](http://localhost:8000)
- frontend on [http://localhost:5173](http://localhost:5173)

First-time setup can also be run from the root:

```bash
npm run setup
```

Run the backend:

```bash
source backend/.venv/bin/activate
python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

## Frontend Setup

```bash
cd /Users/jeffreygatbonton/Desktop/Code/voice_exploration/frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

Optional frontend override:

```bash
VITE_BACKEND_WS_URL=ws://localhost:8000/ws/voice npm run dev
```

## What To Try

Click **Start Call Simulation**, grant microphone access, then try short utterances:

- "What are my options?"
- "I can't pay the full amount."
- "Yes, the 50 dollar plan works."
- "I'm overwhelmed and want to talk to a person."
- "Goodbye."

The UI shows connection status, partial/final transcripts, agent messages, account state, tool calls, latency metrics, provider errors, and session events.

## Architecture Notes

- `VoiceSession` owns one call and starts separate async tasks for browser audio, Deepgram STT, transcript handling, LLM agent turns, TTS, and outbound WebSocket events.
- `InMemorySessionStore` has async `save/get/delete` methods so Redis can replace it later without rewriting the session loop.
- The LLM orchestration is intentionally split into classification, tool decisions, response generation, guardrail validation, and decision logging.
- `create_payment_plan` uses an idempotency key based on the session, user event, amount, and date.
- The ElevenLabs client exposes an async chunk iterator, but the current browser path buffers one full MP3 response before playback for simplicity.
- Barge-in is a placeholder: the browser stops current audio playback when microphone input rises while TTS is playing, then sends a `barge_in` control event to the backend.

## Fake Account

```text
customer_id: demo_123
name: Alex
past_due_amount: 420.00
minimum_payment_today: 50.00
allowed options:
  - pay full balance today
  - pay 50 today and remainder in 14 days
  - hardship callback escalation
```

## Guardrails

`backend/guardrails/policy.py` blocks or rewrites simple regex/string matches for:

- threats
- legal claims
- shame language
- requests for full payment card details
- unsupported amount claims

This is a deterministic demo hook, not a production compliance layer.

## Optional Telephony Placeholders

- `POST /twilio/voice` returns example TwiML with `<Connect><Stream />`.
- `WS /ws/twilio-media` is a skeleton documenting how Twilio Media Streams would connect.
- `integrations/retell_adapter.py` explains how Retell lifecycle events could map into the same session/orchestration layer.

## Known Limitations

- Requires valid Deepgram, ElevenLabs, and OpenAI-compatible credentials for full voice flow.
- Browser audio uses `MediaRecorder` WebM/Opus chunks; production phone audio needs stricter codec handling.
- TTS playback is buffered per response rather than true low-latency chunk playback.
- No persistent database, auth, rate limiting, webhook verification, or production observability.
- Guardrails are intentionally simple and regex-based.
- The fake payment plan is stored in memory only and is demo-only.

## Next Steps

- Redis session state
- Twilio real phone call support
- Retell integration
- proper HMAC webhook verification
- persistent DB
- real observability
- eval suite for agent responses
# ai_voice_exploration
