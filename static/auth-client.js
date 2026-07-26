(function (root, factory) {
  const exported = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  else Object.assign(root, exported);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function fromBase64url(value) {
    const normalized = value.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
    const binary = typeof atob === 'function' ? atob(padded) : Buffer.from(padded, 'base64').toString('binary');
    return Uint8Array.from(binary, char => char.charCodeAt(0)).buffer;
  }

  function toBase64url(buffer) {
    const bytes = new Uint8Array(buffer || new ArrayBuffer(0));
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    const encoded = typeof btoa === 'function' ? btoa(binary) : Buffer.from(binary, 'binary').toString('base64');
    return encoded.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }

  function decodeCredentialOptions(options) {
    const decoded = { ...options, challenge: fromBase64url(options.challenge) };
    if (options.user?.id) decoded.user = { ...options.user, id: fromBase64url(options.user.id) };
    for (const key of ['allowCredentials', 'excludeCredentials']) {
      if (options[key]) decoded[key] = options[key].map(item => ({ ...item, id: fromBase64url(item.id) }));
    }
    return decoded;
  }

  function serializeCredential(credential) {
    const response = credential.response;
    const serialized = {
      id: credential.id,
      rawId: toBase64url(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment || undefined,
      clientExtensionResults: credential.getClientExtensionResults?.() || {},
      response: { clientDataJSON: toBase64url(response.clientDataJSON) },
    };
    for (const key of ['attestationObject', 'authenticatorData', 'signature', 'userHandle']) {
      if (response[key]) serialized.response[key] = toBase64url(response[key]);
    }
    if (typeof response.getTransports === 'function') serialized.response.transports = response.getTransports();
    return serialized;
  }

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

  class AuthGate {
    constructor() { this.status = { enrollment: {}, session: {} }; }
    update(status) { this.status = status || { enrollment: {}, session: {} }; return this; }
    get enrolled() { return Boolean(this.status.enrollment?.complete); }
    get canListen() { return Boolean(this.status.session?.passkey && (!this.status.policy?.face_required || this.status.session?.face)); }
    get canType() { return this.canListen; }
    get canRun() { return Boolean(this.status.session?.authorized); }
  }

  return { AuthGate, decodeCredentialOptions, encodeWav16, fromBase64url, serializeCredential, toBase64url };
});
