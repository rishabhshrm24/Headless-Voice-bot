// Runs on the audio rendering thread. Receives Float32 frames at the
// context's native sample rate, downsamples to 24000 Hz mono, converts to
// PCM16, and posts ~100ms chunks back to the main thread as base64 strings
// via a small side-channel (base64 encoding done in-worklet since atob/btoa
// are available in AudioWorkletGlobalScope).
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = 24000;
    this.nativeRate = sampleRate; // global in AudioWorkletGlobalScope
    this.ratio = this.nativeRate / this.targetRate;
    this.buffer = [];
    this.samplesPerChunk = Math.round(this.targetRate * 0.1); // 100ms
    this.resampleAccumulator = [];
  }

  // Simple linear-interpolation downsampler.
  _downsample(float32Input) {
    const outLength = Math.floor(float32Input.length / this.ratio);
    const out = new Float32Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const srcIndex = i * this.ratio;
      const i0 = Math.floor(srcIndex);
      const i1 = Math.min(i0 + 1, float32Input.length - 1);
      const frac = srcIndex - i0;
      out[i] = float32Input[i0] * (1 - frac) + float32Input[i1] * frac;
    }
    return out;
  }

  _floatToPcm16(float32) {
    const pcm16 = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return pcm16;
  }

  _pcm16ToBase64(pcm16) {
    const bytes = new Uint8Array(pcm16.buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0]; // mono
    if (!channel || channel.length === 0) return true;

    const downsampled = this._downsample(channel);
    this.resampleAccumulator.push(...downsampled);

    while (this.resampleAccumulator.length >= this.samplesPerChunk) {
      const chunk = this.resampleAccumulator.splice(0, this.samplesPerChunk);
      const pcm16 = this._floatToPcm16(Float32Array.from(chunk));
      const base64 = this._pcm16ToBase64(pcm16);
      this.port.postMessage({ type: "chunk", base64 });
    }

    // Also post raw level info every process() call (~2.9ms at 128 frames)
    // for responsive orb animation; downstream code throttles to rAF.
    let sumSquares = 0;
    for (let i = 0; i < channel.length; i++) sumSquares += channel[i] * channel[i];
    const rms = Math.sqrt(sumSquares / channel.length);
    this.port.postMessage({ type: "level", rms });

    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
