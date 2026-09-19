export default class VoiceSocket {
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.handlers = {};
  }

  on(eventType, handler) {
    this.handlers[eventType] = handler;
  }

  _emit(eventType, payload) {
    if (this.handlers[eventType]) this.handlers[eventType](payload);
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => resolve();
      this.ws.onerror = (err) => {
        this._emit("error", { error: err });
        reject(err);
      };
      this.ws.onclose = (event) => {
        this._emit("close", { code: event.code, reason: event.reason });
      };
      this.ws.onmessage = (event) => this._handleMessage(event.data);
    });
  }

  _handleMessage(raw) {
    let data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      return;
    }

    switch (data.type) {
      case "voicebot.session_id":
        this._emit("session_id", { sessionId: data.session_id });
        break;
      case "response.output_audio.delta": {
        // Docs/reference script say this field is "audio", but that was
        // never actually exercised end-to-end before — fall back to
        // "delta" (the field name OpenAI uses on most other streaming
        // delta events) and log if neither is present, so a mismatch is
        // diagnosable from the console instead of crashing atob() on
        // undefined.
        const audioB64 = data.audio ?? data.delta;
        if (typeof audioB64 !== "string") {
          console.warn("response.output_audio.delta had no audio/delta field:", data);
          break;
        }
        this._emit("audio_delta", { audio: audioB64, itemId: data.item_id ?? null });
        break;
      }
      case "input_audio_buffer.speech_started":
        // Server-side VAD heard the caller start talking (barge-in cue).
        this._emit("speech_started", {});
        break;
      case "response.output_audio_transcript.done":
        this._emit("bot_transcript", { transcript: data.transcript, itemId: data.item_id ?? null });
        break;
      case "conversation.item.input_audio_transcription.completed":
        this._emit("user_transcript", { transcript: data.transcript });
        break;
      case "error":
        this._emit("error", { error: data.error });
        break;
      default:
        break; // ignore unknown event types per protocol spec
    }
  }

  sendAudioChunk(base64Pcm16) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: base64Pcm16 }));
  }

  // Send an arbitrary client event (e.g. conversation.item.truncate).
  sendEvent(event) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify(event));
  }

  close() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
