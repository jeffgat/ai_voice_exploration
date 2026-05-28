"""Placeholder notes for a future Twilio Media Streams integration.

Twilio can stream live phone-call audio over a WebSocket using Media Streams.
In a real deployment, the FastAPI app would expose:

- a webhook that returns TwiML with <Connect><Stream url="wss://.../ws/twilio-media" />
- a /ws/twilio-media WebSocket that accepts Twilio media JSON frames
- audio transcoding between Twilio's payload format and provider STT/TTS formats
- HMAC/signature verification for webhooks

This PoC keeps the browser microphone path first so the realtime architecture
is easy to run locally without a public tunnel or a Twilio account.
"""

