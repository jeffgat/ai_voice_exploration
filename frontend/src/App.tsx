import { useCallback, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Clock3,
  Mic,
  PhoneCall,
  PhoneOff,
  Radio,
  Square,
  UserRound,
  Volume2,
  Wifi,
  WifiOff,
  Wrench,
} from "lucide-react";
import { base64ToBlob, getDefaultWebSocketUrl, pickRecorderMimeType } from "./audio";
import type {
  AgentResponseEvent,
  ConnectionStatus,
  ErrorEvent,
  SessionState,
  TimelineMessage,
  ToolCallEvent,
  TtsAudioEvent,
  VoiceEvent,
} from "./types";

const MAX_EVENTS = 80;
const MAX_MESSAGES = 60;
const MAX_TOOLS = 30;

function formatMs(value?: number | null) {
  if (value === null || value === undefined) return "n/a";
  if (value > 1000) return `${(value / 1000).toFixed(2)}s`;
  return `${Math.round(value)}ms`;
}

function formatCurrency(value: unknown) {
  const amount = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(amount)) return "n/a";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(amount);
}

function compactJson(value: unknown) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function StatusPill({ status }: { status: ConnectionStatus }) {
  const online = status === "connected";
  const Icon = online ? Wifi : WifiOff;
  return (
    <span className={`status-pill ${online ? "online" : ""}`}>
      <Icon size={14} />
      {status}
    </span>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function App() {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [sessionState, setSessionState] = useState<SessionState | null>(null);
  const [partialTranscript, setPartialTranscript] = useState("");
  const [messages, setMessages] = useState<TimelineMessage[]>([]);
  const [events, setEvents] = useState<VoiceEvent[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCallEvent[]>([]);
  const [errors, setErrors] = useState<ErrorEvent[]>([]);
  const [micLevel, setMicLevel] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [bytesSent, setBytesSent] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserFrameRef = useRef<number | null>(null);
  const audioQueueRef = useRef<TtsAudioEvent[]>([]);
  const playingAudioRef = useRef<HTMLAudioElement | null>(null);
  const playingUrlRef = useRef<string | null>(null);
  const playingResponseIdRef = useRef<string | null>(null);
  const isPlayingRef = useRef(false);
  const lastBargeInAtRef = useRef(0);

  const account = sessionState?.account ?? {};
  const metrics = sessionState?.metrics;
  const wsUrl = useMemo(() => getDefaultWebSocketUrl(), []);

  const appendEvent = useCallback((event: VoiceEvent) => {
    setEvents((current) => [event, ...current].slice(0, MAX_EVENTS));
  }, []);

  const appendMessage = useCallback((message: TimelineMessage) => {
    setMessages((current) => [...current, message].slice(-MAX_MESSAGES));
  }, []);

  const sendControl = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }, []);

  const markInterrupted = useCallback((responseId: string | null) => {
    if (!responseId) return;
    setMessages((current) =>
      current.map((message) =>
        message.id === responseId
          ? { ...message, interrupted: true, meta: `${message.meta ?? "agent"} · interrupted` }
          : message,
      ),
    );
  }, []);

  const cleanupPlayback = useCallback(() => {
    const audio = playingAudioRef.current;
    if (audio) {
      audio.pause();
      audio.src = "";
    }
    if (playingUrlRef.current) {
      URL.revokeObjectURL(playingUrlRef.current);
    }
    playingAudioRef.current = null;
    playingUrlRef.current = null;
    playingResponseIdRef.current = null;
    isPlayingRef.current = false;
    setIsPlaying(false);
  }, []);

  const playNext = useCallback(() => {
    if (isPlayingRef.current) return;
    const next = audioQueueRef.current.shift();
    if (!next) return;

    const blob = base64ToBlob(next.audio_base64, next.mime_type);
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    playingAudioRef.current = audio;
    playingUrlRef.current = url;
    playingResponseIdRef.current = next.response_event_id;
    isPlayingRef.current = true;
    setIsPlaying(true);

    audio.onended = () => {
      sendControl({ type: "playback_finished", response_event_id: next.response_event_id });
      cleanupPlayback();
      playNext();
    };
    audio.onerror = () => {
      cleanupPlayback();
      playNext();
    };
    audio.play().catch(() => {
      appendMessage({
        id: `local_audio_${Date.now()}`,
        role: "error",
        text: "Browser blocked audio playback. Click Start again or interact with the page before retrying.",
        at: new Date().toISOString(),
      });
      cleanupPlayback();
    });
  }, [appendMessage, cleanupPlayback, sendControl]);

  const enqueueAudio = useCallback(
    (event: TtsAudioEvent) => {
      audioQueueRef.current.push(event);
      playNext();
    },
    [playNext],
  );

  const interruptPlayback = useCallback(() => {
    const responseId = playingResponseIdRef.current;
    if (!responseId) return;
    cleanupPlayback();
    audioQueueRef.current = [];
    markInterrupted(responseId);
    sendControl({ type: "barge_in", response_event_id: responseId });
  }, [cleanupPlayback, markInterrupted, sendControl]);

  const startInputMeter = useCallback(
    (stream: MediaStream) => {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) {
        return;
      }
      const audioContext = new AudioContextClass();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      const source = audioContext.createMediaStreamSource(stream);
      source.connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      audioContextRef.current = audioContext;

      const tick = () => {
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (const sample of data) {
          const centered = (sample - 128) / 128;
          sum += centered * centered;
        }
        const rms = Math.sqrt(sum / data.length);
        const level = Math.min(1, rms * 8);
        setMicLevel(level);

        const now = Date.now();
        if (isPlayingRef.current && level > 0.18 && now - lastBargeInAtRef.current > 1200) {
          lastBargeInAtRef.current = now;
          interruptPlayback();
        }
        analyserFrameRef.current = requestAnimationFrame(tick);
      };
      tick();
    },
    [interruptPlayback],
  );

  const stopLocalMedia = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      recorderRef.current.stop();
    }
    recorderRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (analyserFrameRef.current) {
      cancelAnimationFrame(analyserFrameRef.current);
      analyserFrameRef.current = null;
    }
    audioContextRef.current?.close();
    audioContextRef.current = null;
    setMicLevel(0);
  }, []);

  const handleVoiceEvent = useCallback(
    (event: VoiceEvent) => {
      appendEvent(event);
      if (event.type === "session_state") {
        setSessionState(event.state);
        setPartialTranscript(event.state.latest_partial_transcript);
        setMessages((current) =>
          current.map((message) =>
            event.state.interrupted_agent_event_ids.includes(message.id)
              ? { ...message, interrupted: true }
              : message,
          ),
        );
      }
      if (event.type === "partial_transcript") {
        setPartialTranscript(event.text);
      }
      if (event.type === "final_transcript") {
        setPartialTranscript("");
        appendMessage({
          id: event.event_id,
          role: "user",
          text: event.text,
          meta: `STT ${formatMs(event.stt_latency_ms)}`,
          at: event.created_at,
        });
      }
      if (event.type === "agent_response") {
        const agentEvent = event as AgentResponseEvent;
        appendMessage({
          id: agentEvent.event_id,
          role: "agent",
          text: agentEvent.text,
          meta: `${agentEvent.intent} · LLM ${formatMs(agentEvent.llm_latency_ms)}`,
          interrupted: agentEvent.interrupted,
          at: agentEvent.created_at,
        });
      }
      if (event.type === "tts_audio") {
        enqueueAudio(event);
      }
      if (event.type === "tool_call") {
        setToolCalls((current) => [event, ...current].slice(0, MAX_TOOLS));
      }
      if (event.type === "error") {
        setErrors((current) => [event, ...current].slice(0, 10));
        appendMessage({
          id: event.event_id,
          role: "error",
          text: event.message,
          meta: event.recoverable ? "recoverable" : "fatal",
          at: event.created_at,
        });
      }
    },
    [appendEvent, appendMessage, enqueueAudio],
  );

  const startRecorder = useCallback((stream: MediaStream) => {
    const recorder = new MediaRecorder(stream, pickRecorderMimeType());
    recorder.ondataavailable = async (event) => {
      if (!event.data.size) return;
      const ws = wsRef.current;
      if (ws?.readyState !== WebSocket.OPEN) return;
      const buffer = await event.data.arrayBuffer();
      ws.send(buffer);
      setBytesSent((current) => current + buffer.byteLength);
    };
    recorder.start(250);
    recorderRef.current = recorder;
  }, []);

  const startCall = useCallback(async () => {
    if (status === "connected" || status === "connecting" || status === "requesting-mic") return;
    setStatus("requesting-mic");
    setMessages([]);
    setEvents([]);
    setToolCalls([]);
    setErrors([]);
    setSessionState(null);
    setBytesSent(0);
    audioQueueRef.current = [];
    cleanupPlayback();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      streamRef.current = stream;
      setStatus("connecting");

      const ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;
      ws.onopen = () => {
        setStatus("connected");
        sendControl({ type: "client_ready" });
        startRecorder(stream);
        startInputMeter(stream);
      };
      ws.onmessage = (message) => {
        const event = JSON.parse(message.data as string) as VoiceEvent;
        handleVoiceEvent(event);
      };
      ws.onerror = () => {
        setStatus("error");
      };
      ws.onclose = () => {
        setStatus((current) => (current === "stopping" ? "closed" : current === "error" ? "error" : "closed"));
        stopLocalMedia();
        cleanupPlayback();
      };
    } catch (error) {
      setStatus("error");
      appendMessage({
        id: `local_start_${Date.now()}`,
        role: "error",
        text: error instanceof Error ? error.message : "Unable to start microphone capture.",
        at: new Date().toISOString(),
      });
      stopLocalMedia();
    }
  }, [
    appendMessage,
    cleanupPlayback,
    handleVoiceEvent,
    sendControl,
    startInputMeter,
    startRecorder,
    status,
    stopLocalMedia,
    wsUrl,
  ]);

  const stopCall = useCallback(() => {
    setStatus("stopping");
    sendControl({ type: "stop_call" });
    stopLocalMedia();
    cleanupPlayback();
    wsRef.current?.close();
    wsRef.current = null;
  }, [cleanupPlayback, sendControl, stopLocalMedia]);

  const isActive = status === "connected" || status === "connecting" || status === "requesting-mic";

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>Realtime Voice Agent PoC</h1>
          <p>Demo-only payment arrangement call console</p>
        </div>
        <StatusPill status={status} />
      </header>

      <main className="workspace">
        <aside className="panel control-panel">
          <div className="panel-heading">
            <PhoneCall size={18} />
            <h2>Call Simulation</h2>
          </div>
          <div className="button-row">
            <button className="primary-button" onClick={startCall} disabled={isActive}>
              <PhoneCall size={18} />
              Start Call Simulation
            </button>
            <button className="ghost-button" onClick={stopCall} disabled={!isActive}>
              <PhoneOff size={18} />
              Stop
            </button>
          </div>

          <div className="signal-stack">
            <div className="signal-row">
              <Mic size={16} />
              <span>Mic level</span>
              <div className="meter">
                <i style={{ width: `${Math.round(micLevel * 100)}%` }} />
              </div>
            </div>
            <div className="signal-row">
              <Radio size={16} />
              <span>Bytes sent</span>
              <strong>{bytesSent.toLocaleString()}</strong>
            </div>
            <div className="signal-row">
              <Volume2 size={16} />
              <span>Playback</span>
              <strong>{isPlaying ? "playing" : "idle"}</strong>
            </div>
          </div>

          <div className="demo-notice">
            <AlertTriangle size={16} />
            <span>No real payments. No real collection activity.</span>
          </div>

          <section className="mini-section">
            <h3>Latency</h3>
            <div className="metric-grid">
              <Metric label="STT" value={formatMs(metrics?.stt_latency_ms)} />
              <Metric label="LLM" value={formatMs(metrics?.llm_latency_ms)} />
              <Metric label="TTS" value={formatMs(metrics?.tts_latency_ms)} />
              <Metric label="Total" value={formatMs(metrics?.total_response_latency_ms)} />
            </div>
          </section>
        </aside>

        <section className="panel transcript-panel">
          <div className="panel-heading spread">
            <div>
              <div className="inline-title">
                <Clock3 size={18} />
                <h2>Transcript</h2>
              </div>
              <p>{sessionState?.session_id ?? "No active session"}</p>
            </div>
            <span className="intent-pill">{sessionState?.last_intent ?? "waiting"}</span>
          </div>

          <div className="partial-box">
            <span>Partial</span>
            <p>{partialTranscript || "..."}</p>
          </div>

          <div className="message-list">
            {messages.length === 0 ? (
              <div className="empty-state">Start a call to see the live transcript and agent turns.</div>
            ) : (
              messages.map((message) => (
                <article key={message.id} className={`message ${message.role} ${message.interrupted ? "interrupted" : ""}`}>
                  <div className="avatar">
                    {message.role === "user" ? <UserRound size={16} /> : message.role === "agent" ? <Bot size={16} /> : <AlertTriangle size={16} />}
                  </div>
                  <div>
                    <div className="message-meta">
                      <strong>{message.role}</strong>
                      <span>{message.meta}</span>
                    </div>
                    <p>{message.text}</p>
                    {message.interrupted && <em>Playback interrupted by barge-in placeholder.</em>}
                  </div>
                </article>
              ))
            )}
          </div>
        </section>

        <aside className="inspector">
          <section className="panel account-panel">
            <div className="panel-heading">
              <CheckCircle2 size={18} />
              <h2>Fake Account State</h2>
            </div>
            <dl className="account-grid">
              <div>
                <dt>Customer</dt>
                <dd>{String(account.name ?? "Alex")}</dd>
              </div>
              <div>
                <dt>Past due</dt>
                <dd>{formatCurrency(account.past_due_amount ?? 420)}</dd>
              </div>
              <div>
                <dt>Minimum today</dt>
                <dd>{formatCurrency(account.minimum_payment_today ?? 50)}</dd>
              </div>
              <div>
                <dt>Escalated</dt>
                <dd>{sessionState?.escalated ? "yes" : "no"}</dd>
              </div>
            </dl>
            <div className="plan-list">
              {(sessionState?.allowed_payment_plans ?? []).map((plan) => (
                <div key={String(plan.plan_id)} className="plan-row">
                  <span>{String(plan.label)}</span>
                  <strong>{formatCurrency(plan.amount_today)}</strong>
                </div>
              ))}
            </div>
          </section>

          <section className="panel tool-panel">
            <div className="panel-heading">
              <Wrench size={18} />
              <h2>Tool Calls</h2>
            </div>
            <div className="tool-list">
              {toolCalls.length === 0 ? (
                <div className="empty-state small">No tool calls yet.</div>
              ) : (
                toolCalls.map((tool) => (
                  <details key={tool.event_id} className={`tool-call ${tool.status}`}>
                    <summary>
                      <span>{tool.tool_name}</span>
                      <strong>{tool.status}</strong>
                    </summary>
                    <pre>{compactJson({ input: tool.input, output: tool.output, latency_ms: tool.latency_ms })}</pre>
                  </details>
                ))
              )}
            </div>
          </section>

          <section className="panel event-panel">
            <div className="panel-heading">
              <Square size={18} />
              <h2>Event Timeline</h2>
            </div>
            <div className="event-list">
              {events.map((event) => (
                <div key={event.event_id} className="event-row">
                  <span>{event.type}</span>
                  <time>{new Date(event.created_at).toLocaleTimeString()}</time>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </main>
    </div>
  );
}

export default App;
