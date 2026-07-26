(function (root, factory) {
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  root.VoiceQueue = exported.VoiceQueue;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  class VoiceQueue {
    constructor(options = {}) {
      this.audioFactory = options.audioFactory || (() => new Audio());
      this.fetchAudio = options.fetchAudio;
      this.fallbackSpeaker = options.fallbackSpeaker || null;
      this.cancelFallback = options.cancelFallback || (() => {});
      this.onIdle = options.onIdle || (() => {});
      this.queue = [];
      this.current = null;
      this.currentRunId = null;
      this.suppressedRunId = null;
      this.pumping = false;
      this.generation = 0;
    }
    beginRun(runId) {
      this.kill(this.currentRunId);
      this.currentRunId = runId;
      this.suppressedRunId = null;
    }
    enqueue(text, runId = this.currentRunId) {
      const clean = String(text || '').trim();
      if (!clean || !runId || runId === this.suppressedRunId) return false;
      this.queue.push({ text: clean, runId });
      this._pump();
      return true;
    }
    get pendingCount() { return this.queue.length; }
    kill(runId = this.currentRunId) {
      if (runId) this.suppressedRunId = runId;
      this.queue.length = 0;
      this.generation += 1;
      this.cancelFallback();
      if (this.current) {
        this.current.onended = null;
        this.current.onerror = null;
        try { this.current.pause(); } catch (_) {}
        if (this.current.__url && typeof URL !== 'undefined') URL.revokeObjectURL(this.current.__url);
      }
      this.current = null;
      this.pumping = false;
    }
    async _pump() {
      if (this.pumping) return;
      this.pumping = true;
      const generation = this.generation;
      try {
        while (this.queue.length && generation === this.generation) {
          const item = this.queue.shift();
          if (item.runId === this.suppressedRunId) continue;
          try {
            const source = await this.fetchAudio(item.text);
            if (generation !== this.generation || item.runId === this.suppressedRunId) break;
            const audio = this.audioFactory();
            this.current = audio;
            let url = source;
            if (typeof Blob !== 'undefined' && source instanceof Blob) url = URL.createObjectURL(source);
            if ('src' in audio) audio.src = url;
            audio.__url = (typeof url === 'string' && url.startsWith('blob:')) ? url : null;
            await new Promise((resolve, reject) => {
              audio.onended = resolve;
              audio.onerror = reject;
              Promise.resolve(audio.play()).catch(reject);
            });
            if (audio.__url && typeof URL !== 'undefined') URL.revokeObjectURL(audio.__url);
            this.current = null;
          } catch (error) {
            if (generation !== this.generation) break;
            if (this.fallbackSpeaker && item.runId !== this.suppressedRunId) await this.fallbackSpeaker(item.text);
          }
        }
      } finally {
        if (generation === this.generation) {
          this.current = null;
          this.pumping = false;
          if (this.queue.length) this._pump();
          else this.onIdle(this.currentRunId);
        }
      }
    }
  }
  return { VoiceQueue };
});
