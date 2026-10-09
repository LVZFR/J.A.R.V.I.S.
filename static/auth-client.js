(function (root, factory) {
  const exported = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  else Object.assign(root, exported);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function encodeWav16(samples, sampleRate = 16000) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const text = (offset, value) => { for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i)); };
    text(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); text(8, 'WAVE'); text(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i += 1) {
      const value = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, Math.round(value < 0 ? value * 32768 : value * 32767), true);
    }
    return buffer;
  }

  // Simplified shared-secret auth gate (no WebAuthn/Touch ID, no biometrics).
  // Kept the same shape the rest of app.js expects: canListen/canType/canRun.
  class AuthGate {
    constructor() { this.status = { session: {} }; }
    update(status) { this.status = status || { session: {} }; return this; }
    get authorized() { return Boolean(this.status.session?.authorized); }
    get canListen() { return this.authorized; }
    get canType() { return this.authorized; }
    get canRun() { return this.authorized; }
  }

  return { AuthGate, encodeWav16 };
});
