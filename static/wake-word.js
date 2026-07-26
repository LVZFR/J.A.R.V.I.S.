(function (root, factory) {
  const exported = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  else Object.assign(root, exported);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  class WakeWordGate {
    constructor(onWake) {
      this.onWake = onWake;
      this.armed = false;
      this.triggered = false;
    }

    setArmed(enabled) {
      this.armed = Boolean(enabled);
      this.triggered = false;
    }

    reset() {
      this.triggered = false;
    }

    handleDetection(payload) {
      if (!this.armed || this.triggered || !payload?.detected) return false;
      this.triggered = true;
      this.onWake(payload);
      return true;
    }
  }

  function downsampleTo16k(input, inputRate) {
    if (inputRate < 16000) throw new Error('Microphone sample rate must be at least 16 kHz');
    if (inputRate === 16000) return new Float32Array(input);
    const ratio = inputRate / 16000;
    const outputLength = Math.floor(input.length / ratio);
    const output = new Float32Array(outputLength);
    for (let i = 0; i < outputLength; i += 1) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j += 1) sum += input[j];
      output[i] = sum / Math.max(1, end - start);
    }
    return output;
  }

  class PcmFrameBuffer {
    constructor(frameSamples = 1280) {
      this.frameSamples = frameSamples;
      this.pending = [];
    }

    get pendingSamples() { return this.pending.length; }

    push(samples) {
      this.pending.push(...samples);
      const frames = [];
      while (this.pending.length >= this.frameSamples) {
        const floats = this.pending.splice(0, this.frameSamples);
        const pcm = new Int16Array(this.frameSamples);
        for (let i = 0; i < floats.length; i += 1) {
          const value = Math.max(-1, Math.min(1, floats[i]));
          pcm[i] = Math.round(value < 0 ? value * 32768 : value * 32767);
        }
        frames.push(pcm.buffer);
      }
      return frames;
    }

    clear() { this.pending.length = 0; }
  }

  return { WakeWordGate, downsampleTo16k, PcmFrameBuffer };
});
