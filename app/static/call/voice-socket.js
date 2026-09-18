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
      case "response.output_audio.delta":
        this._emit("audio_delta", { audio: data.audio });
        break;
      case "response.output_audio_transcript.done":
        this._emit("bot_transcript", { transcript: data.transcript });
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

  close() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
