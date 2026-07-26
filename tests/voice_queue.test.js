const assert = require('assert');
const { VoiceQueue } = require('../static/voice-queue.js');

function deferredAudioFactory(log) {
  return () => {
    let ended;
    const end = new Promise(resolve => { ended = resolve; });
    const audio = {
      onended: null,
      onerror: null,
      play() { log.active += 1; log.maxActive = Math.max(log.maxActive, log.active); return end; },
      pause() { log.active = Math.max(0, log.active - 1); },
      finish() { log.active = Math.max(0, log.active - 1); if (audio.onended) audio.onended(); ended(); },
    };
    log.instances.push(audio);
    return audio;
  };
}

(async () => {
  const log = { active: 0, maxActive: 0, instances: [], idleCalls: 0 };
  const queue = new VoiceQueue({ audioFactory: deferredAudioFactory(log), fetchAudio: async text => text, onIdle: () => { log.idleCalls += 1; } });
  queue.beginRun('run-a');
  queue.enqueue('one', 'run-a');
  queue.enqueue('two', 'run-a');
  await new Promise(r => setTimeout(r, 0));
  assert.equal(log.instances.length, 1, 'second chunk must wait for first audio.onended');
  log.instances[0].finish();
  await new Promise(r => setTimeout(r, 0));
  assert.equal(log.instances.length, 2, 'second chunk starts only after first ends');
  assert.equal(log.maxActive, 1, 'queued audio must never overlap');
  log.instances[1].finish();
  await new Promise(r => setTimeout(r, 0));
  assert.equal(log.idleCalls, 1, 'queue announces when all speech has finished');

  queue.kill('run-a');
  assert.equal(queue.suppressedRunId, 'run-a');
  queue.enqueue('future', 'run-a');
  await new Promise(r => setTimeout(r, 0));
  assert.equal(log.instances.length, 2, 'kill must suppress all future chunks for current run');
  assert.equal(queue.pendingCount, 0, 'kill must clear queued chunks');
  console.log('voice queue regression: PASS');
})().catch(err => { console.error(err); process.exit(1); });
