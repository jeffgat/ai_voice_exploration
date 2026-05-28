export type ConnectionStatus = "idle" | "requesting-mic" | "connecting" | "connected" | "stopping" | "closed" | "error";

export type LatencyMetrics = {
  stt_latency_ms?: number | null;
  llm_latency_ms?: number | null;
  tts_latency_ms?: number | null;
  total_response_latency_ms?: number | null;
};

export type SessionState = {
  session_id: string;
  customer_id: string;
  status: string;
  started_at: string;
  ended_at?: string | null;
  account: Record<string, unknown>;
  allowed_payment_plans: Array<Record<string, unknown>>;
  created_payment_plans: Array<Record<string, unknown>>;
  latest_partial_transcript: string;
  last_intent?: string | null;
  active_agent_event_id?: string | null;
  interrupted_agent_event_ids: string[];
  escalated: boolean;
  escalation_reason?: string | null;
  ended: boolean;
  call_summary?: string | null;
  audio_chunks_received: number;
  audio_bytes_received: number;
  events_emitted: number;
  metrics: LatencyMetrics;
};

export type BaseEvent = {
  type: string;
  event_id: string;
  session_id: string;
  created_at: string;
};

export type AudioChunkEvent = BaseEvent & {
  type: "audio_chunk";
  size_bytes: number;
  chunks_received: number;
  total_bytes_received: number;
};

export type PartialTranscriptEvent = BaseEvent & {
  type: "partial_transcript";
  text: string;
  stt_latency_ms?: number | null;
};

export type FinalTranscriptEvent = BaseEvent & {
  type: "final_transcript";
  text: string;
  stt_latency_ms?: number | null;
};

export type AgentResponseEvent = BaseEvent & {
  type: "agent_response";
  text: string;
  intent: string;
  user_event_id?: string | null;
  llm_latency_ms?: number | null;
  guardrail_reasons: string[];
  interrupted: boolean;
};

export type TtsAudioEvent = BaseEvent & {
  type: "tts_audio";
  response_event_id: string;
  audio_base64: string;
  mime_type: string;
  sequence: number;
  is_final: boolean;
  tts_latency_ms: number;
  total_response_latency_ms: number;
};

export type ToolCallEvent = BaseEvent & {
  type: "tool_call";
  tool_name: string;
  status: "success" | "error" | "interrupted";
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  latency_ms?: number | null;
};

export type ErrorEvent = BaseEvent & {
  type: "error";
  message: string;
  recoverable: boolean;
  details: Record<string, unknown>;
};

export type SessionStateEvent = BaseEvent & {
  type: "session_state";
  state: SessionState;
};

export type VoiceEvent =
  | AudioChunkEvent
  | PartialTranscriptEvent
  | FinalTranscriptEvent
  | AgentResponseEvent
  | TtsAudioEvent
  | ToolCallEvent
  | ErrorEvent
  | SessionStateEvent;

export type TimelineMessage = {
  id: string;
  role: "user" | "agent" | "system" | "error";
  text: string;
  meta?: string;
  interrupted?: boolean;
  at: string;
};

