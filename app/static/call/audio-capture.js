export default class AudioCapture {
  constructor() {
    this.audioContext = null;
    this.workletNode = null;
    this.mediaStream = null;
    this.muted = false;
    this._onChunk = null;
    this._onLevel = null;
    this._latestLevel = 0;
    this._rafId = null;
  }

  async start({ onChunk, onLevel }) {
    try {
      this._onChunk = onChunk;
      this._onLevel = onLevel;

      this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.audioContext = new AudioContext();
      await this.audioContext.audioWorklet.addModule("/static/call/audio-worklet-capture.js");

      const source = this.audioContext.createMediaStreamSource(this.mediaStream);
      this.workletNode = new AudioWorkletNode(this.audioContext, "capture-processor");

      this.workletNode.port.onmessage = (event) => {
        const { type } = event.data;
        if (type === "chunk") {
          if (!this.muted && this._onChunk) this._onChunk(event.data.base64);
        } else if (type === "level") {
          this._latestLevel = event.data.rms;
        }
      };

      source.connect(this.workletNode);
      // Worklet doesn't need to reach speakers; connect to a muted destination
      // path is unnecessary since we never connect workletNode to context.destination.

      this._tickLevel();
    } catch (error) {
      if (this.mediaStream) {
        this.mediaStream.getTracks().forEach((track) => track.stop());
      }
      this.mediaStream = null;
      this.audioContext = null;
      throw error;
    }
  }

  _tickLevel() {
    if (this._onLevel) this._onLevel(Math.min(1, this._latestLevel * 6));
    this._rafId = requestAnimationFrame(() => this._tickLevel());
  }

  setMuted(muted) {
    this.muted = muted;
  }

  stop() {
    if (this._rafId) cancelAnimationFrame(this._rafId);
    if (this.workletNode) this.workletNode.port.onmessage = null;
    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((track) => track.stop());
    }
    if (this.audioContext) {
      this.audioContext.close();
    }
    this.audioContext = null;
    this.workletNode = null;
    this.mediaStream = null;
  }
}
