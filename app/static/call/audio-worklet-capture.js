// Runs on the audio rendering thread. Receives Float32 frames at the
// context's native sample rate, downsamples to 24000 Hz mono, converts to
// PCM16, and posts ~100ms chunks back to the main thread as base64 strings.
// btoa/atob are NOT available in AudioWorkletGlobalScope (unlike Window or
// DedicatedWorkerGlobalScope), so base64 encoding is done manually below.
const BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

function uint8ToBase64(bytes) {
  let result = "";
  const len = bytes.length;
  for (let i = 0; i < len; i += 3) {
    const b1 = bytes[i];
    const b2 = i + 1 < len ? bytes[i + 1] : 0;
    const b3 = i + 2 < len ? bytes[i + 2] : 0;
    const triplet = (b1 << 16) | (b2 << 8) | b3;
    result += BASE64_CHARS[(triplet >> 18) & 0x3f];
    result += BASE64_CHARS[(triplet >> 12) & 0x3f];
    result += i + 1 < len ? BASE64_CHARS[(triplet >> 6) & 0x3f] : "=";
    result += i + 2 < len ? BASE64_CHARS[triplet & 0x3f] : "=";
  }
  return result;
}

class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = 24000;
    this.nativeRate = sampleRate; // global in AudioWorkletGlobalScope
    this.ratio = this.nativeRate / this.targetRate;
    this.buffer = [];
    this.samplesPerChunk = Math.round(this.targetRate * 0.1); // 100ms
    this.resampleAccumulator = [];

    // Persistent state for the linear-interpolation downsampler so the
    // fractional read position (and the one sample of lookback interpolation
    // needs across a block boundary) survives across process() calls instead
    // of resetting every ~128-sample block. Without this, non-integer ratios
    // (e.g. 44100/24000) produce a discontinuity at every block boundary.
    this._carry = new Float32Array(1); // last raw input sample from the previous block
    this._phase = 0; // fractional position (in carry+block coords) of the next output sample
  }

  // Linear-interpolation downsampler with persistent fractional phase across
  // process() calls (see constructor comment).
  _downsample(float32Input) {
    const buf = new Float32Array(1 + float32Input.length);
    buf[0] = this._carry[0];
    buf.set(float32Input, 1);

    const out = [];
    let pos = this._phase;
    while (pos + 1 < buf.length) {
      const i0 = Math.floor(pos);
      const i1 = i0 + 1;
      const frac = pos - i0;
      out.push(buf[i0] * (1 - frac) + buf[i1] * frac);
      pos += this.ratio;
    }

    // Carry the last raw sample of this block forward, and re-express the
    // leftover fractional phase relative to that carried sample so the next
    // call picks up exactly where this one left off.
    this._carry[0] = buf[buf.length - 1];
    this._phase = pos - (buf.length - 1);

    return Float32Array.from(out);
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
    return uint8ToBase64(new Uint8Array(pcm16.buffer));
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
