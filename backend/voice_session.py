from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from backend.llm.orchestrator import LLMOrchestrator
from backend.settings import Settings
from backend.stt.deepgram import DeepgramStreamingSTT, TranscriptResult
from backend.tts.elevenlabs import ElevenLabsTTS


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


class LatencyMetrics(BaseModel):
    stt_latency_ms: int | None = None
    llm_latency_ms: int | None = None
    tts_latency_ms: int | None = None
    total_response_latency_ms: int | None = None


class CallSessionState(BaseModel):
    session_id: str
    customer_id: str = "demo_123"
    status: Literal["starting", "active", "stopping", "ended", "error"] = "starting"
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    account: dict[str, Any] = Field(default_factory=dict)
    allowed_payment_plans: list[dict[str, Any]] = Field(default_factory=list)
    created_payment_plans: list[dict[str, Any]] = Field(default_factory=list)
    payment_plans_by_key: dict[str, dict[str, Any]] = Field(default_factory=dict)
    conversation: list[dict[str, Any]] = Field(default_factory=list)
    decision_log: list[dict[str, Any]] = Field(default_factory=list)
    latest_partial_transcript: str = ""
    last_user_event_id: str | None = None
    last_intent: str | None = None
    active_agent_event_id: str | None = None
    interrupted_agent_event_ids: list[str] = Field(default_factory=list)
    escalated: bool = False
    escalation_reason: str | None = None
    ended: bool = False
    call_summary: str | None = None
    audio_chunks_received: int = 0
    audio_bytes_received: int = 0
    events_emitted: int = 0
    metrics: LatencyMetrics = Field(default_factory=LatencyMetrics)


class SessionSnapshot(BaseModel):
    session_id: str
    customer_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    account: dict[str, Any]
    allowed_payment_plans: list[dict[str, Any]]
    created_payment_plans: list[dict[str, Any]]
    latest_partial_transcript: str
    last_intent: str | None
    active_agent_event_id: str | None
    interrupted_agent_event_ids: list[str]
    escalated: bool
    escalation_reason: str | None
    ended: bool
    call_summary: str | None
    audio_chunks_received: int
    audio_bytes_received: int
    events_emitted: int
    metrics: LatencyMetrics


class EventBase(BaseModel):
    type: str
    event_id: str = Field(default_factory=new_event_id)
    session_id: str
    created_at: datetime = Field(default_factory=utc_now)


class AudioChunkEvent(EventBase):
    type: Literal["audio_chunk"] = "audio_chunk"
    size_bytes: int
    chunks_received: int
    total_bytes_received: int


class PartialTranscriptEvent(EventBase):
    type: Literal["partial_transcript"] = "partial_transcript"
    text: str
    stt_latency_ms: int | None = None


class FinalTranscriptEvent(EventBase):
    type: Literal["final_transcript"] = "final_transcript"
    text: str
    stt_latency_ms: int | None = None


class AgentResponseEvent(EventBase):
    type: Literal["agent_response"] = "agent_response"
    text: str
    intent: str
    user_event_id: str | None = None
    llm_latency_ms: int | None = None
    guardrail_reasons: list[str] = Field(default_factory=list)
    interrupted: bool = False


class TtsAudioEvent(EventBase):
    type: Literal["tts_audio"] = "tts_audio"
    response_event_id: str
    audio_base64: str
    mime_type: str = "audio/mpeg"
    sequence: int = 0
    is_final: bool = True
    tts_latency_ms: int
    total_response_latency_ms: int


class ToolCallEvent(EventBase):
    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    status: Literal["success", "error", "interrupted"]
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = None


class ErrorEvent(EventBase):
    type: Literal["error"] = "error"
    message: str
    recoverable: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class SessionStateEvent(EventBase):
    type: Literal["session_state"] = "session_state"
    state: SessionSnapshot


OutboundEvent = (
    AudioChunkEvent
    | PartialTranscriptEvent
    | FinalTranscriptEvent
    | AgentResponseEvent
    | TtsAudioEvent
    | ToolCallEvent
    | ErrorEvent
    | SessionStateEvent
)


class SessionStore(Protocol):
    async def save(self, state: CallSessionState) -> None: ...
    async def get(self, session_id: str) -> CallSessionState | None: ...
    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """Tiny async store with a Redis-shaped boundary.

    The methods are async so a Redis implementation can be swapped in later
    without changing the WebSocket/session orchestration code.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, CallSessionState] = {}
        self._lock = asyncio.Lock()

    async def save(self, state: CallSessionState) -> None:
        async with self._lock:
            self._sessions[state.session_id] = state

    async def get(self, session_id: str) -> CallSessionState | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)


class VoiceSession:
    """Owns one browser call simulation and its async task graph."""

    def __init__(self, websocket: WebSocket, settings: Settings, store: SessionStore) -> None:
        self.websocket = websocket
        self.settings = settings
        self.store = store
        self.state = CallSessionState(session_id=f"sess_{uuid.uuid4().hex[:10]}")
        self.stop_event = asyncio.Event()
        self.audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=120)
        self.transcript_queue: asyncio.Queue[TranscriptResult] = asyncio.Queue()
        self.agent_queue: asyncio.Queue[FinalTranscriptEvent] = asyncio.Queue()
        self.outgoing: asyncio.Queue[OutboundEvent] = asyncio.Queue()
        self.deepgram = DeepgramStreamingSTT(settings)
        self.orchestrator = LLMOrchestrator(settings)
        self.tts = ElevenLabsTTS(settings)
        self.current_utterance_started_at: float | None = None

    async def run(self) -> None:
        await self.websocket.accept()
        self.state.status = "active"
        await self.store.save(self.state)

        long_running_tasks = [
            asyncio.create_task(self._send_outbound(), name="send_outbound"),
            asyncio.create_task(self._receive_browser_audio(), name="receive_browser_audio"),
            asyncio.create_task(self._stream_to_deepgram(), name="stream_to_deepgram"),
            asyncio.create_task(self._handle_transcripts(), name="handle_transcripts"),
            asyncio.create_task(self._run_agent_turns(), name="run_agent_turns"),
        ]
        opening_task = asyncio.create_task(self._send_opening_turn(), name="send_opening_turn")

        await self._emit_session_state()

        try:
            done, pending = await asyncio.wait(long_running_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    await self._emit_error(f"Session task failed: {exc}", recoverable=False)
            self.stop_event.set()
            if not opening_task.done():
                opening_task.cancel()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(opening_task, return_exceptions=True)
        finally:
            self.state.status = "ended"
            self.state.ended_at = utc_now()
            await self.store.save(self.state)
            self._signal_audio_close()
            await self.store.delete(self.state.session_id)

    async def _send_opening_turn(self) -> None:
        result = await self.orchestrator.initial_response(self.state, self._log_tool_call)
        await self._emit_agent_turn(result, user_event_id=None, stt_latency_ms=None)

    async def _receive_browser_audio(self) -> None:
        try:
            while not self.stop_event.is_set():
                message = await self.websocket.receive()
                if message.get("bytes") is not None:
                    await self._handle_audio_chunk(message["bytes"])
                elif message.get("text") is not None:
                    await self._handle_client_message(message["text"])
                elif message.get("type") == "websocket.disconnect":
                    self.stop_event.set()
                    return
        except WebSocketDisconnect:
            self.stop_event.set()

    async def _handle_audio_chunk(self, chunk: bytes) -> None:
        if self.current_utterance_started_at is None:
            self.current_utterance_started_at = time.perf_counter()

        self.state.audio_chunks_received += 1
        self.state.audio_bytes_received += len(chunk)
        if self.audio_queue.full():
            # Drop the oldest chunk under backpressure. For production phone audio
            # you would tune queues and provider reconnect behavior more carefully.
            _ = self.audio_queue.get_nowait()
        await self.audio_queue.put(chunk)

        if self.state.audio_chunks_received % 4 == 0:
            await self._emit(
                AudioChunkEvent(
                    session_id=self.state.session_id,
                    size_bytes=len(chunk),
                    chunks_received=self.state.audio_chunks_received,
                    total_bytes_received=self.state.audio_bytes_received,
                )
            )

    async def _handle_client_message(self, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            await self._emit_error("Received malformed client control message.", details={"text": text})
            return

        message_type = message.get("type")
        if message_type == "stop_call":
            self.state.status = "stopping"
            self.state.ended = True
            self.stop_event.set()
            self._signal_audio_close()
            await self._emit_session_state()
        elif message_type == "barge_in":
            response_event_id = message.get("response_event_id") or self.state.active_agent_event_id
            if response_event_id and response_event_id not in self.state.interrupted_agent_event_ids:
                self.state.interrupted_agent_event_ids.append(response_event_id)
            self.state.active_agent_event_id = None
            await self._log_tool_call(
                "barge_in_placeholder",
                {"response_event_id": response_event_id},
                {"message": "Frontend stopped current audio playback while user spoke."},
                0,
                "interrupted",
            )
            await self._emit_session_state()
        elif message_type == "playback_finished":
            if message.get("response_event_id") == self.state.active_agent_event_id:
                self.state.active_agent_event_id = None
            await self._emit_session_state()
        elif message_type == "client_ready":
            await self._emit_session_state()

    async def _stream_to_deepgram(self) -> None:
        try:
            await self.deepgram.stream(self.audio_queue, self.transcript_queue, self.stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(str(exc), recoverable=True, details={"provider": "deepgram"})
            await self.stop_event.wait()

    async def _handle_transcripts(self) -> None:
        while not self.stop_event.is_set():
            result = await self.transcript_queue.get()
            stt_latency_ms = self._calculate_stt_latency(result)
            if result.is_final:
                self.state.latest_partial_transcript = ""
                event = FinalTranscriptEvent(
                    session_id=self.state.session_id,
                    text=result.text,
                    stt_latency_ms=stt_latency_ms,
                )
                self.state.last_user_event_id = event.event_id
                self.state.conversation.append(
                    {"role": "user", "text": result.text, "event_id": event.event_id, "at": utc_now().isoformat()}
                )
                await self._emit(event)
                await self.agent_queue.put(event)
                self.current_utterance_started_at = None
            else:
                self.state.latest_partial_transcript = result.text
                await self._emit(
                    PartialTranscriptEvent(
                        session_id=self.state.session_id,
                        text=result.text,
                        stt_latency_ms=stt_latency_ms,
                    )
                )
            await self._emit_session_state()

    async def _run_agent_turns(self) -> None:
        while not self.stop_event.is_set():
            transcript_event = await self.agent_queue.get()
            started = time.perf_counter()
            result = await self.orchestrator.handle_user_transcript(
                transcript_event.text,
                self.state,
                self._log_tool_call,
            )
            total_so_far_ms = int((time.perf_counter() - started) * 1000)
            await self._emit_agent_turn(
                result,
                user_event_id=transcript_event.event_id,
                stt_latency_ms=transcript_event.stt_latency_ms,
                turn_started_at=started,
                total_so_far_ms=total_so_far_ms,
            )

    async def _emit_agent_turn(
        self,
        result: Any,
        user_event_id: str | None,
        stt_latency_ms: int | None,
        turn_started_at: float | None = None,
        total_so_far_ms: int = 0,
    ) -> None:
        agent_event = AgentResponseEvent(
            session_id=self.state.session_id,
            text=result.text,
            intent=result.intent,
            user_event_id=user_event_id,
            llm_latency_ms=result.llm_latency_ms,
            guardrail_reasons=result.guardrail_reasons,
        )
        self.state.last_intent = result.intent
        self.state.active_agent_event_id = agent_event.event_id
        self.state.metrics.stt_latency_ms = stt_latency_ms
        self.state.metrics.llm_latency_ms = result.llm_latency_ms
        self.state.conversation.append(
            {
                "role": "assistant",
                "text": result.text,
                "event_id": agent_event.event_id,
                "at": utc_now().isoformat(),
            }
        )
        await self._emit(agent_event)

        tts_started = time.perf_counter()
        try:
            audio = await self.tts.synthesize_to_buffer(result.text)
            tts_latency_ms = int((time.perf_counter() - tts_started) * 1000)
            total_response_latency_ms = (
                int((time.perf_counter() - turn_started_at) * 1000)
                if turn_started_at
                else total_so_far_ms + tts_latency_ms
            )
            self.state.metrics.tts_latency_ms = tts_latency_ms
            self.state.metrics.total_response_latency_ms = total_response_latency_ms
            await self._emit(
                TtsAudioEvent(
                    session_id=self.state.session_id,
                    response_event_id=agent_event.event_id,
                    audio_base64=base64.b64encode(audio).decode("ascii"),
                    tts_latency_ms=tts_latency_ms,
                    total_response_latency_ms=total_response_latency_ms,
                )
            )
        except Exception as exc:
            await self._emit_error(
                f"TTS failed for the latest agent response: {exc}",
                recoverable=True,
                details={"provider": "elevenlabs", "response_event_id": agent_event.event_id},
            )
        await self._emit_session_state()

    def _calculate_stt_latency(self, result: TranscriptResult) -> int | None:
        if result.provider_latency_ms is not None:
            return result.provider_latency_ms
        if self.current_utterance_started_at is None:
            return None
        return int((time.perf_counter() - self.current_utterance_started_at) * 1000)

    async def _log_tool_call(
        self,
        tool_name: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        latency_ms: int,
        status: Literal["success", "error", "interrupted"],
    ) -> None:
        await self._emit(
            ToolCallEvent(
                session_id=self.state.session_id,
                tool_name=tool_name,
                input=input_payload,
                output=output_payload,
                latency_ms=latency_ms,
                status=status,
            )
        )
        await self._emit_session_state()

    async def _emit_error(
        self,
        message: str,
        recoverable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.state.status = "error" if not recoverable else self.state.status
        await self._emit(
            ErrorEvent(
                session_id=self.state.session_id,
                message=message,
                recoverable=recoverable,
                details=details or {},
            )
        )
        await self._emit_session_state()

    async def _emit_session_state(self) -> None:
        await self.store.save(self.state)
        await self._emit(SessionStateEvent(session_id=self.state.session_id, state=self._snapshot()))

    async def _emit(self, event: OutboundEvent) -> None:
        self.state.events_emitted += 1
        await self.outgoing.put(event)

    async def _send_outbound(self) -> None:
        while not self.stop_event.is_set():
            event = await self.outgoing.get()
            await self.websocket.send_json(event.model_dump(mode="json"))

    def _signal_audio_close(self) -> None:
        if self.audio_queue.full():
            _ = self.audio_queue.get_nowait()
        try:
            self.audio_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def _snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self.state.session_id,
            customer_id=self.state.customer_id,
            status=self.state.status,
            started_at=self.state.started_at,
            ended_at=self.state.ended_at,
            account=self.state.account,
            allowed_payment_plans=self.state.allowed_payment_plans,
            created_payment_plans=self.state.created_payment_plans,
            latest_partial_transcript=self.state.latest_partial_transcript,
            last_intent=self.state.last_intent,
            active_agent_event_id=self.state.active_agent_event_id,
            interrupted_agent_event_ids=self.state.interrupted_agent_event_ids,
            escalated=self.state.escalated,
            escalation_reason=self.state.escalation_reason,
            ended=self.state.ended,
            call_summary=self.state.call_summary,
            audio_chunks_received=self.state.audio_chunks_received,
            audio_bytes_received=self.state.audio_bytes_received,
            events_emitted=self.state.events_emitted,
            metrics=self.state.metrics,
        )
