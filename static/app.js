'use strict';

const $ = id => document.getElementById(id);
const state = { armed: '', runId: null, startedAt: 0, phrase: '', recorder: null, stream: null, wakeAvailable: false, runComplete: true };
const logEl = $('actionLog');
const wake = { enabled: false, ready: false, paused: false, socket: null, stream: null, context: null, source: null, processor: null, reconnectTimer: null, frames: new PcmFrameBuffer(1280) };
const wakeGate = new WakeWordGate(activateWakeWord);
const authGate = new AuthGate();

function log(message, tone = '') {
  const row = document.createElement('div'); row.className = `log-entry ${tone}`;
  row.innerHTML = `<time>${new Date().toLocaleTimeString()}</time>${escapeHtml(message)}`;
  logEl.prepend(row); while (logEl.children.length > 80) logEl.lastChild.remove();
}
function escapeHtml(text) { const d = document.createElement('div'); d.textContent = String(text); return d.innerHTML; }
function toast(text) { $('toast').textContent = text; $('toast').classList.add('show'); setTimeout(() => $('toast').classList.remove('show'), 1800); }
function setCore(text, recording = false) { $('coreState').textContent = text; document.querySelector('.reactor-wrap').classList.toggle('recording', recording); }

async function apiJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail; try { detail = await response.json(); } catch (_) { detail = await response.text(); }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail.detail || detail));
  }
  return response.json();
}

function renderAuth(status) {
  authGate.update(status);
  const authorized = Boolean(status.session?.authorized);
  $('accessState').textContent = authorized ? 'UNLOCKED' : 'LOCKED';
  $('authBadge').textContent = `IDENTITY · ${authorized ? 'AUTHORIZED' : 'LOCKED'}`;
  $('authBadge').classList.toggle('authorized', authorized);
  if (!authorized) { stopWakeMode(); $('authModal').classList.remove('hidden'); $('authProgress').textContent = 'SESSION LOCKED'; }
  else { $('authModal').classList.add('hidden'); $('authProgress').textContent = 'SESSION UNLOCKED'; }
}

async function refreshAuthStatus() {
  try { const status = await apiJson('/api/auth/status'); renderAuth(status); return status; }
  catch (e) { log(`Identity status error: ${e.message}`, 'error'); return null; }
}

async function login(secret) {
  $('authProgress').textContent = 'VERIFYING ACCESS KEY';
  try {
    await apiJson('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({secret})});
    $('authSecret').value = '';
    log('Access key accepted', 'amber');
    await refreshAuthStatus();
  } catch (e) {
    $('authProgress').textContent = 'ACCESS DENIED';
    log(`Login error: ${e.message}`, 'error');
    toast('Incorrect access key');
  }
}

async function lockIdentity() {
  stopWakeMode();
  try { await apiJson('/api/auth/logout', {method:'POST'}); } catch (_) {}
  $('authModal').classList.remove('hidden'); $('authProgress').textContent = 'SESSION LOCKED'; await refreshAuthStatus(); log('Session locked', 'error');
}

function browserSpeak(text) {
  return new Promise(resolve => {
    if (!('speechSynthesis' in window)) return resolve();
    const u = new SpeechSynthesisUtterance(text); u.rate = 1.06; u.pitch = .92;
    u.onend = resolve; u.onerror = resolve; speechSynthesis.speak(u);
  });
}
const voiceQueue = new VoiceQueue({
  fetchAudio: async text => {
    const response = await fetch('/api/tts', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({text}) });
    if (!response.ok) throw new Error('server TTS unavailable');
    return response.blob();
  },
  fallbackSpeaker: browserSpeak,
  cancelFallback: () => { if ('speechSynthesis' in window) speechSynthesis.cancel(); },
  onIdle: runId => { if (state.runComplete && state.runId === runId) resumeWakeMode(); }
});

function feedVoice(delta, final = false) {
  if (!delta && !final) return;
  state.phrase += delta || '';
  const boundary = /^(.*?[.!?;:\n](?:\s|$))(.*)$/s.exec(state.phrase);
  if (boundary && boundary[1].trim().length >= 18) { voiceQueue.enqueue(boundary[1], state.runId); state.phrase = boundary[2]; }
  else if (state.phrase.length >= 130) { const cut = state.phrase.lastIndexOf(' ', 110); const at = cut > 30 ? cut : 110; voiceQueue.enqueue(state.phrase.slice(0, at), state.runId); state.phrase = state.phrase.slice(at); }
  if (final && state.phrase.trim()) { voiceQueue.enqueue(state.phrase, state.runId); state.phrase = ''; }
}

function renderWakeState(label, mode = '') {
  $('wakeword').textContent = label.toUpperCase();
  $('wakeButton').textContent = `${wake.enabled ? '◆' : '◇'} WAKE MODE · ${label.toUpperCase()}`;
  $('wakeButton').classList.toggle('active', wake.enabled && mode !== 'loading');
  $('wakeButton').classList.toggle('loading', mode === 'loading');
  $('wakeButton').setAttribute('aria-pressed', String(wake.enabled));
}

function connectWakeSocket() {
  if (!wake.enabled) return;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  wake.socket = new WebSocket(`${scheme}://${location.host}/api/wakeword`);
  wake.socket.binaryType = 'arraybuffer';
  wake.socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.event === 'wakeword.loading') { renderWakeState('loading', 'loading'); log('Loading local Hey Jarvis model…', 'amber'); }
    else if (message.event === 'wakeword.ready') { wake.ready = true; wakeGate.setArmed(!wake.paused); renderWakeState(wake.paused ? 'paused' : 'armed'); log('Wake mode armed · say “Hey Jarvis”', 'amber'); }
    else if (message.event === 'wakeword.detected') wakeGate.handleDetection(message);
    else if (message.event === 'wakeword.error') { log(`Wake-word error: ${message.error}`, 'error'); renderWakeState('error'); }
  };
  wake.socket.onclose = () => {
    wake.ready = false;
    if (wake.enabled) {
      renderWakeState('reconnecting', 'loading');
      clearTimeout(wake.reconnectTimer);
      wake.reconnectTimer = setTimeout(connectWakeSocket, 1800);
    }
  };
}

async function startWakeMode() {
  if (!authGate.canListen) { $('authModal').classList.remove('hidden'); return toast('Unlock JARVIS first'); }
  if (!state.wakeAvailable) return toast('Local wake-word engine unavailable');
  if (!navigator.mediaDevices?.getUserMedia || !window.WebSocket) return toast('Wake mode unsupported in this browser');
  try {
    wake.stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    wake.context = new AudioContext(); await wake.context.resume();
    wake.source = wake.context.createMediaStreamSource(wake.stream);
    wake.processor = wake.context.createScriptProcessor(4096, 1, 1);
    wake.source.connect(wake.processor); wake.processor.connect(wake.context.destination);
    wake.processor.onaudioprocess = event => {
      if (!wake.enabled || wake.paused || !wake.ready || wake.socket?.readyState !== WebSocket.OPEN) return;
      const samples = downsampleTo16k(event.inputBuffer.getChannelData(0), wake.context.sampleRate);
      for (const frame of wake.frames.push(samples)) wake.socket.send(frame);
    };
    wake.enabled = true; wake.paused = false; wakeGate.setArmed(true); renderWakeState('loading', 'loading'); connectWakeSocket();
    log('Wake mode enabled · microphone processing stays local', 'amber');
  } catch (e) { log(`Wake mode error: ${e.message}`, 'error'); toast('Microphone permission required'); stopWakeMode(); }
}

function stopWakeMode() {
  wake.enabled = false; wake.ready = false; wake.paused = false; wakeGate.setArmed(false); wake.frames.clear(); clearTimeout(wake.reconnectTimer);
  if (wake.socket) { wake.socket.onclose = null; wake.socket.close(); }
  if (wake.processor) { wake.processor.onaudioprocess = null; wake.processor.disconnect(); }
  if (wake.source) wake.source.disconnect();
  if (wake.stream) wake.stream.getTracks().forEach(track => track.stop());
  if (wake.context) wake.context.close();
  wake.socket = wake.stream = wake.context = wake.source = wake.processor = null;
  renderWakeState(state.wakeAvailable ? 'off' : 'unavailable');
}

function pauseWakeMode() {
  if (!wake.enabled) return;
  wake.paused = true; wakeGate.setArmed(false); wake.frames.clear(); renderWakeState('paused');
}

function resumeWakeMode() {
  if (!wake.enabled) return;
  wake.paused = false; wakeGate.setArmed(wake.ready); wake.frames.clear(); renderWakeState(wake.ready ? 'armed' : 'loading', wake.ready ? '' : 'loading');
}

function playWakeChime() {
  if (!wake.context) return;
  const oscillator = wake.context.createOscillator(), gain = wake.context.createGain(), now = wake.context.currentTime;
  oscillator.frequency.setValueAtTime(660, now); oscillator.frequency.exponentialRampToValueAtTime(990, now + .13);
  gain.gain.setValueAtTime(.0001, now); gain.gain.exponentialRampToValueAtTime(.12, now + .02); gain.gain.exponentialRampToValueAtTime(.0001, now + .18);
  oscillator.connect(gain); gain.connect(wake.context.destination); oscillator.start(now); oscillator.stop(now + .2);
}

function activateWakeWord(message) {
  pauseWakeMode(); playWakeChime(); setCore('AWAITING COMMAND', true);
  log(`Wake phrase detected · confidence ${Math.round((message.score || 0) * 100)}%`, 'amber');
  setTimeout(() => recordVoice(true), 260);
}

async function refreshStatus() {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    $('profile').textContent = String(s.profile || 'default').toUpperCase();
    $('stt').textContent = s.stt.available ? (s.stt.primary || s.stt.fallback || 'READY').toUpperCase() : 'UNAVAILABLE';
    $('tts').textContent = (s.tts.provider || 'UNKNOWN').toUpperCase();
    state.wakeAvailable = Boolean(s.wakeword?.available);
    if (!wake.enabled) renderWakeState(state.wakeAvailable ? 'off' : 'unavailable');
    $('gateway').textContent = s.gateway.online ? 'ONLINE' : 'OFFLINE'; $('latency').textContent = s.gateway.latency_ms != null ? `${s.gateway.latency_ms} MS` : '— MS';
    $('gatewayBadge').textContent = `GATEWAY · ${s.gateway.online ? 'ONLINE' : 'OFFLINE'}`; $('gatewayBadge').className = `badge ${s.gateway.online ? 'online' : 'offline'}`;
    log(`Status: gateway ${s.gateway.status}; STT ${s.stt.available ? 'ready' : 'unavailable'}; TTS ${s.tts.provider}`);
  } catch (e) { $('gateway').textContent = 'BRIDGE ERROR'; $('gatewayBadge').className = 'badge offline'; log(`Status error: ${e.message}`, 'error'); }
}

function armCommand(command) {
  state.armed = state.armed === command ? '' : command;
  $('armedCommand').textContent = state.armed || 'NONE';
  document.querySelectorAll('[data-command]').forEach(b => b.classList.toggle('selected', b.dataset.command === state.armed));
  $('prompt').placeholder = state.armed ? `Instructions for ${state.armed}...` : 'Type an instruction or select a command, then speak...';
  log(state.armed ? `Command armed: ${state.armed}` : 'Command deck disarmed', 'amber'); $('prompt').focus();
}

async function submitDirective(rawText, inputMode = 'typed') {
  const text = String(rawText || '').trim(); if (!text && !state.armed) return;
  if (!authGate.canRun) { $('authModal').classList.remove('hidden'); toast('Unlock JARVIS first'); return; }
  const directive = state.armed ? `${state.armed}${text ? ` ${text}` : ''}` : text;
  $('prompt').value = ''; state.armed = ''; $('armedCommand').textContent = 'NONE'; document.querySelectorAll('[data-command]').forEach(b => b.classList.remove('selected'));
  pauseWakeMode();
  $('response').textContent = ''; $('sendButton').disabled = true; setCore('LINKING'); state.startedAt = performance.now(); state.phrase = ''; state.runComplete = false;
  log(`Directive transmitted: ${directive}`, 'amber');
  try {
    const sessionKey = localStorage.jarvisSessionKey || (localStorage.jarvisSessionKey = `jarvis:web:${crypto.randomUUID()}`);
    const response = await fetch('/api/run', { method:'POST', headers:{'Content-Type':'application/json','X-Jarvis-Session-Key':sessionKey}, body:JSON.stringify({input:directive, input_mode:inputMode, session_id:sessionKey}) });
    if (!response.ok) throw new Error(await response.text());
    const run = await response.json(); state.runId = run.run_id; voiceQueue.beginRun(state.runId); $('runLabel').textContent = state.runId.slice(0,18).toUpperCase(); setCore('PROCESSING');
    log(`Run started: ${state.runId}`); await streamRun(state.runId);
  } catch (e) { state.runComplete = true; log(`Run error: ${e.message}`, 'error'); $('response').textContent += `\n[ERROR] ${e.message}`; setCore('FAULT'); $('sendButton').disabled = false; resumeWakeMode(); }
}

async function streamRun(runId) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`); if (!response.ok) throw new Error(await response.text());
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
    while (true) {
      const {done, value} = await reader.read(); if (done) break; buffer += decoder.decode(value, {stream:true});
      const packets = buffer.split('\n\n'); buffer = packets.pop();
      for (const packet of packets) for (const line of packet.split('\n')) if (line.startsWith('data:')) {
        try { handleEvent(JSON.parse(line.slice(5).trim())); } catch (e) { log(`SSE parse error: ${e.message}`, 'error'); }
      }
    }
  } catch (e) { log(`Stream error: ${e.message}`, 'error'); }
  finally { $('sendButton').disabled = false; }
}

function handleEvent(event) {
  const type = event.event || 'unknown';
  if (type === 'message.delta') { $('response').textContent += event.delta || ''; $('response').scrollTop = $('response').scrollHeight; feedVoice(event.delta || ''); return; }
  if (type === 'tool.started') { log(`Tool started: ${event.tool}${event.preview ? ` · ${event.preview}` : ''}`); return; }
  if (type === 'tool.completed') { log(`Tool completed: ${event.tool} · ${event.duration || 0}s${event.error ? ' · ERROR' : ''}`, event.error ? 'error' : ''); return; }
  if (type === 'approval.request') { log('Approval required', 'amber'); showApproval(event); return; }
  if (type === 'approval.responded') { log(`Approval response: ${event.choice}`, 'amber'); return; }
  if (type === 'run.completed') { if (!$('response').textContent && event.output) $('response').textContent = event.output; state.runComplete = true; feedVoice('', true); setCore('STANDBY'); log(`Run complete · ${Math.round(performance.now()-state.startedAt)} ms total`, 'amber'); if (!voiceQueue.pumping || voiceQueue.suppressedRunId === state.runId) resumeWakeMode(); return; }
  if (type === 'run.failed' || type === 'bridge.error') { state.runComplete = true; setCore('FAULT'); resumeWakeMode(); log(`${type}: ${event.error}`, 'error'); return; }
  if (type === 'run.cancelled') { state.runComplete = true; setCore('STANDBY'); resumeWakeMode(); log('Run cancelled', 'error'); return; }
  log(`Event: ${type}`);
}

function showApproval(event) {
  $('approvalDescription').textContent = event.description || event.reason || 'Hermes requests authorization for this operation.';
  $('approvalCommand').textContent = event.command || event.tool || JSON.stringify(event, null, 2);
  const choices = Array.isArray(event.choices) && event.choices.length ? event.choices : ['once','deny']; $('approvalChoices').replaceChildren();
  choices.forEach(raw => { const choice = typeof raw === 'string' ? raw : (raw.value || raw.choice); if (!choice) return; const b=document.createElement('button'); b.textContent=choice.toUpperCase(); b.className=choice==='deny'?'deny':''; b.onclick=()=>sendApproval(choice); $('approvalChoices').appendChild(b); });
  $('approvalModal').classList.remove('hidden');
}
async function sendApproval(choice) {
  try { const r=await fetch('/api/approval',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:state.runId,choice})}); if(!r.ok)throw new Error(await r.text()); $('approvalModal').classList.add('hidden'); log(`Approval ${choice}`,choice==='deny'?'error':'amber'); }
  catch(e){toast('Approval failed');log(`Approval error: ${e.message}`,'error');}
}

async function recordVoice(wakeTriggered = false) {
  if (state.recorder) { state.recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia) return toast('Microphone unavailable');
  pauseWakeMode();
  try {
    const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}}), chunks=[];
    const mime=['audio/webm;codecs=opus','audio/mp4','audio/webm'].find(type => MediaRecorder.isTypeSupported(type)) || '';
    const recorder=new MediaRecorder(stream,mime?{mimeType:mime}:undefined); state.recorder=recorder; recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
    const ctx=new AudioContext(), source=ctx.createMediaStreamSource(stream), analyser=ctx.createAnalyser(), processor=ctx.createScriptProcessor(4096,1,1), voiceChunks=[], data=new Uint8Array(512); analyser.fftSize=1024; source.connect(analyser); source.connect(processor); processor.connect(ctx.destination); processor.onaudioprocess=e=>voiceChunks.push(downsampleTo16k(e.inputBuffer.getChannelData(0),ctx.sampleRate));
    let started=performance.now(),heardSpeech=false,silentSince=0,raf=0; const silenceMs=550,minRecordMs=450,maxRecordMs=12000;
    const monitor=()=>{ analyser.getByteTimeDomainData(data); let sum=0; for(const n of data){const v=(n-128)/128;sum+=v*v} const rms=Math.sqrt(sum/data.length); drawWave(data,rms); const now=performance.now();
      if(rms>.025){heardSpeech=true;silentSince=0}else if(heardSpeech&&!silentSince)silentSince=now;
      if((heardSpeech&&now-started>minRecordMs&&silentSince&&now-silentSince>=silenceMs)||now-started>=maxRecordMs)recorder.stop();else raf=requestAnimationFrame(monitor); };
    recorder.onstop=async()=>{ cancelAnimationFrame(raf); processor.onaudioprocess=null;processor.disconnect();source.disconnect();stream.getTracks().forEach(t=>t.stop()); await ctx.close(); state.recorder=null; $('micButton').classList.remove('recording'); $('voiceState').textContent='VERIFYING'; setCore('VERIFYING VOICE');
      const blob=new Blob(chunks,{type:recorder.mimeType||'audio/webm'}); log(`Voice captured · ${Math.round(performance.now()-started)} ms · ${Math.round(blob.size/1024)} KB`);
      if(!heardSpeech){$('voiceState').textContent='IDLE';setCore('STANDBY');log('No command speech detected after wake phrase','error');resumeWakeMode();return;}
      const form=new FormData(); form.append('audio',blob,recorder.mimeType.includes('mp4')?'recording.mp4':'recording.webm'); const sttStart=performance.now();
      try{$('voiceState').textContent='TRANSCRIBING';setCore('TRANSCRIBING');const r=await fetch('/api/stt',{method:'POST',body:form});if(!r.ok)throw new Error(await r.text());const out=await r.json();$('prompt').value=out.text;log(`STT ${out.provider} · ${out.latency_ms||Math.round(performance.now()-sttStart)} ms: ${out.text}`,'amber');$('voiceState').textContent='IDLE';await submitDirective(out.text,'voice')}catch(e){setCore('LOCKED');$('voiceState').textContent='REJECTED';log(`Voice pipeline stopped: ${e.message}`,'error');toast('Command failed');await refreshAuthStatus();resumeWakeMode()}
    };
    recorder.start(100); $('micButton').classList.add('recording'); $('voiceState').textContent=wakeTriggered?'COMMAND':'LISTENING'; setCore(wakeTriggered?'SPEAK COMMAND':'LISTENING',true); log(wakeTriggered?'Wake accepted · listening for command':'Voice recording started'); monitor();
  } catch(e){log(`Microphone error: ${e.message}`,'error');toast('Microphone permission required');resumeWakeMode();}
}
function drawWave(data,rms=0){const c=$('waveform'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);x.strokeStyle=rms>.025?'#ffc34d':'#58edff';x.shadowColor=x.strokeStyle;x.shadowBlur=7;x.beginPath();for(let i=0;i<data.length;i++){const px=i/(data.length-1)*c.width,py=(data[i]/255)*c.height;i?x.lineTo(px,py):x.moveTo(px,py)}x.stroke()}
function idleWave(){if(state.recorder)return;const data=new Uint8Array(160);const t=Date.now()/500;for(let i=0;i<data.length;i++)data[i]=128+Math.sin(i/7+t)*5+Math.sin(i/19-t)*3;drawWave(data);requestAnimationFrame(idleWave)}

$('promptForm').addEventListener('submit',e=>{e.preventDefault();submitDirective($('prompt').value)}); $('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submitDirective(e.target.value)}});
$('micButton').onclick=()=>recordVoice(false); $('wakeButton').onclick=()=>wake.enabled?stopWakeMode():startWakeMode(); $('killVoice').onclick=()=>{voiceQueue.kill(state.runId);state.phrase='';log(`Voice killed for ${state.runId||'current run'}; text stream remains active`,'error');toast('VOICE OUTPUT TERMINATED')};
$('authForm').addEventListener('submit',e=>{e.preventDefault();const secret=$('authSecret').value.trim();if(secret)login(secret)}); $('authBadge').onclick=()=>$('authModal').classList.remove('hidden'); $('lockAuth').onclick=lockIdentity;
window.addEventListener('beforeunload',()=>{stopWakeMode()});
document.querySelectorAll('[data-command]').forEach(b=>b.onclick=()=>armCommand(b.dataset.command));
(function initLogToggle(){
  const panel = document.querySelector('.log-panel'), btn = $('toggleLog');
  const apply = hidden => { panel.classList.toggle('collapsed', hidden); btn.textContent = hidden ? 'SHOW' : 'HIDE'; };
  apply(localStorage.jarvisLogHidden === '1');
  btn.onclick = () => { const hidden = !panel.classList.contains('collapsed'); apply(hidden); localStorage.jarvisLogHidden = hidden ? '1' : '0'; };
})();
setInterval(()=>$('clock').textContent=new Date().toLocaleTimeString('en-GB'),1000); refreshAuthStatus();refreshStatus();setInterval(refreshStatus,30000);setInterval(refreshAuthStatus,10000);idleWave();log('JARVIS interface initialized');
