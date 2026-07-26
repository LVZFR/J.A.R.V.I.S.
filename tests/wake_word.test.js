const assert = require('assert');
const { WakeWordGate, downsampleTo16k, PcmFrameBuffer } = require('../static/wake-word.js');

let activations = 0;
const gate = new WakeWordGate(() => { activations += 1; });

assert.equal(gate.handleDetection({ detected: true }), false, 'disarmed gate ignores detections');
gate.setArmed(true);
assert.equal(gate.handleDetection({ detected: false }), false, 'sub-threshold frames do not activate');
assert.equal(gate.handleDetection({ detected: true, wake_word: 'hey_jarvis' }), true, 'armed gate activates');
assert.equal(gate.handleDetection({ detected: true }), false, 'gate triggers only once until reset');
assert.equal(activations, 1);
gate.reset();
assert.equal(gate.handleDetection({ detected: true }), true, 'reset rearms an enabled gate');
gate.setArmed(false);
assert.equal(gate.handleDetection({ detected: true }), false, 'disabled gate stays quiet');
assert.equal(activations, 2);

const reduced = downsampleTo16k(new Float32Array([0, 1, 0, 0, -1, 0]), 48000);
assert.equal(reduced.length, 2, '48 kHz input is reduced to 16 kHz');
assert.ok(Math.abs(reduced[0] - 1 / 3) < 0.001);
assert.ok(Math.abs(reduced[1] + 1 / 3) < 0.001);

const frames = new PcmFrameBuffer(4);
assert.equal(frames.push(new Float32Array([0, 0.5, -0.5])).length, 0);
const ready = frames.push(new Float32Array([1, -1]));
assert.equal(ready.length, 1, 'PCM is emitted only in exact model-sized frames');
assert.deepEqual(Array.from(new Int16Array(ready[0])), [0, 16384, -16384, 32767]);
assert.equal(frames.pendingSamples, 1);
console.log('wake word gate regression: PASS');
