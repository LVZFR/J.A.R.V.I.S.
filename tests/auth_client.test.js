const assert = require('assert');
const { AuthGate, decodeCredentialOptions, encodeWav16 } = require('../static/auth-client.js');

const gate = new AuthGate();
gate.update({ enrollment: { complete: true }, session: { passkey: true, face: true, voice: false, reason: 'voice_required' } });
assert.equal(gate.canListen, true, 'wake listening requires passkey and face');
assert.equal(gate.canRun, false, 'commands require a voice match');
gate.update({ enrollment: { complete: true }, session: { passkey: true, face: true, voice: true, authorized: true } });
assert.equal(gate.canRun, true);

gate.update({ policy: { face_required: false }, enrollment: { complete: true }, session: { passkey: true, face: false, voice: false, reason: 'voice_required' } });
assert.equal(gate.canListen, true, 'wake listening requires only passkey when face authentication is disabled');
assert.equal(gate.canType, true, 'typed chat requires the unlocked Touch ID session, not a voice sample');
assert.equal(gate.canRun, false, 'spoken commands still require a fresh voice match');

const options = decodeCredentialOptions({ challenge: 'AQID', user: { id: 'BAUG' }, allowCredentials: [{ id: 'BwgJ', type: 'public-key' }] });
assert.deepEqual(Array.from(new Uint8Array(options.challenge)), [1, 2, 3]);
assert.deepEqual(Array.from(new Uint8Array(options.user.id)), [4, 5, 6]);
assert.deepEqual(Array.from(new Uint8Array(options.allowCredentials[0].id)), [7, 8, 9]);

const wav = encodeWav16(new Float32Array([0, 1, -1]), 16000);
const view = new DataView(wav);
assert.equal(String.fromCharCode(...new Uint8Array(wav, 0, 4)), 'RIFF');
assert.equal(view.getUint32(24, true), 16000);
assert.deepEqual(Array.from(new Int16Array(wav, 44)), [0, 32767, -32768]);
console.log('auth client regression: PASS');
