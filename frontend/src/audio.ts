export function base64ToBlob(base64: string, mimeType: string): Blob {
  const byteCharacters = atob(base64);
  const byteNumbers = new Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i += 1) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  return new Blob([new Uint8Array(byteNumbers)], { type: mimeType });
}

export function pickRecorderMimeType(): MediaRecorderOptions | undefined {
  const preferredTypes = ["audio/webm;codecs=opus", "audio/webm"];
  const mimeType = preferredTypes.find((candidate) => MediaRecorder.isTypeSupported(candidate));
  return mimeType ? { mimeType } : undefined;
}

export function getDefaultWebSocketUrl(): string {
  const explicit = import.meta.env.VITE_BACKEND_WS_URL as string | undefined;
  if (explicit) return explicit;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.hostname}:8000/ws/voice`;
}

