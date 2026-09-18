export default class AudioPlayback {
  constructor() {
    this.audioContext = null;
    this.nextStartTime = 0;
    this._levelCallback = null;
    this._currentLevel = 0;
    this._rafId = null;
  }

  start() {
    if (!this.audioContext) {
      this.audioContext = new AudioContext({ sampleRate: 24000 });
      this.nextStartTime = this.audioContext.currentTime;
    }
    // Browsers can hand back (or later suspend) an AudioContext outside a
    // direct user-gesture call stack — e.g. after the async gap of a mic
    // permission prompt and a WebSocket handshake. Explicitly resuming is
    // a harmless no-op when already running, and is required in some
    // browsers (notably Safari) for the context to ever produce sound.
    if (this.audioContext.state === "suspended") {
      this.audioContext.resume().catch(() => {});
    }
    this._tickLevel();
  }

  onLevel(callback) {
    this._levelCallback = callback;
  }

  _base64ToPcm16(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Int16Array(bytes.buffer);
  }

  _pcm16ToFloat32(pcm16) {
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) {
      float32[i] = pcm16[i] / (pcm16[i] < 0 ? 0x8000 : 0x7fff);
    }
    return float32;
  }

  enqueue(base64Chunk) {
    if (!this.audioContext) return;
    let pcm16;
    try {
      pcm16 = this._base64ToPcm16(base64Chunk);
    } catch (e) {
      console.warn("Dropping malformed audio chunk:", e);
      return;
    }
    const float32 = this._pcm16ToFloat32(pcm16);

    let sumSquares = 0;
    for (let i = 0; i < float32.length; i++) sumSquares += float32[i] * float32[i];
    this._currentLevel = Math.min(1, Math.sqrt(sumSquares / float32.length) * 6);

    const buffer = this.audioContext.createBuffer(1, float32.length, 24000);
    buffer.copyToChannel(float32, 0);

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audioContext.destination);

    const now = this.audioContext.currentTime;
    const startAt = Math.max(now, this.nextStartTime);
    source.start(startAt);
    this.nextStartTime = startAt + buffer.duration;

    source.onended = () => {
      if (this.audioContext && this.audioContext.currentTime >= this.nextStartTime - 0.01) {
        this._currentLevel = 0;
      }
    };
  }

  _tickLevel() {
    if (this._levelCallback) this._levelCallback(this._currentLevel);
    this._rafId = requestAnimationFrame(() => this._tickLevel());
  }

  stop() {
    if (this._rafId) cancelAnimationFrame(this._rafId);
    if (this.audioContext) {
      this.audioContext.close();
    }
    this.audioContext = null;
    this.nextStartTime = 0;
    this._currentLevel = 0;
  }
}
