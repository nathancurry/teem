(() => {
  'use strict';
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});

  const refreshLabel = document.getElementById('refresh-status');
  const runList = document.getElementById('run-list');
  const runStatus = document.getElementById('run-status');
  const runMatch = location.pathname.match(/^\/runs\/([0-9a-f-]{36})$/);
  let lastSeen = '';
  let refreshing = false;

  function setActionsEnabled(enabled) {
    document.querySelectorAll('form button').forEach(button => { button.disabled = !enabled; });
  }

  async function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      const response = await fetch(runMatch ? `${location.pathname}/state` : '/state', { cache: 'no-store' });
      if (!response.ok) throw new Error('refresh failed');
      const state = await response.json();
      lastSeen = new Date(state.refreshed_at).toLocaleTimeString();
      if (refreshLabel) refreshLabel.textContent = `Current as of ${lastSeen}`;
      setActionsEnabled(true);
      if (runStatus && state.runs[0] && runStatus.dataset.status !== state.runs[0].status) {
        location.reload();
        return;
      }
      if (runList) {
        runList.replaceChildren();
        for (const run of state.runs) {
          const item = document.createElement('li');
          const link = document.createElement('a');
          link.href = `/runs/${run.id}`;
          link.textContent = `${run.project}: ${run.objective}`;
          item.append(link, document.createTextNode(` — ${run.label}, round ${run.round}. ${run.summary}`));
          if (run.stop_reason) item.append(document.createTextNode(`. Stop reason: ${run.stop_reason}`));
          runList.append(item);
        }
      }
    } catch (_) {
      if (refreshLabel) refreshLabel.textContent = `Last seen ${lastSeen || 'unknown'} — refresh required before acting`;
      setActionsEnabled(false);
    } finally {
      refreshing = false;
    }
  }
  refresh();
  setInterval(refresh, 5000);
  addEventListener('focus', refresh);
  addEventListener('online', refresh);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

  document.querySelectorAll('form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const values = new URLSearchParams(new FormData(form));
    if (event.submitter?.name) values.set(event.submitter.name, event.submitter.value);
    const button = event.submitter;
    if (button) button.disabled = true;
    try {
      const response = await fetch(form.action, { method: 'POST', body: values,
        credentials: 'same-origin', cache: 'no-store' });
      if (response.redirected && new URL(response.url).origin === location.origin) {
        location.assign(response.url);
        return;
      }
      await refresh();
      if (refreshLabel) refreshLabel.textContent = response.status === 409 ?
        'Decision changed. Review the current Run before acting.' :
        `Submission was not accepted (${response.status}). Review the form and current state.`;
    } catch (_) {
      await refresh();
      if (refreshLabel) refreshLabel.textContent = 'Submission not confirmed. Check recent Runs before retrying this form.';
    } finally {
      if (button && (!refreshLabel || !refreshLabel.textContent.includes('refresh required'))) button.disabled = false;
    }
  }));

  const recordingStatus = document.getElementById('recording-status');
  const micButtons = [...document.querySelectorAll('.mic')];
  let active = null;
  let pressedPointer = false;
  let pressStarted = 0;

  function status(message) { if (recordingStatus) recordingStatus.textContent = message; }
  function stopTracks(capture) {
    if (capture.stream) capture.stream.getTracks().forEach(track => track.stop());
    capture.stream = null;
  }
  function cancel() {
    const capture = active;
    active = null;
    if (capture) {
      clearTimeout(capture.timer);
      if (capture.upload) capture.upload.abort();
      if (capture.recorder && capture.recorder.state !== 'inactive') capture.recorder.stop();
      capture.chunks = [];
      stopTracks(capture);
    }
    micButtons.forEach(button => button.setAttribute('aria-pressed', 'false'));
    status('Recording stopped. Type or record again.');
  }

  async function start(button) {
    cancel();
    const field = document.getElementById(button.dataset.field);
    const capture = { field, version: Number(field.dataset.editVersion || 0), chunks: [],
      stream: null, recorder: null, timer: null, upload: null };
    active = capture;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      status('Recording unavailable. Type your text instead.');
      return;
    }
    const mimeType = ['audio/webm;codecs=opus', 'audio/mp4;codecs=mp4a.40.2', 'audio/mp4']
      .find(type => MediaRecorder.isTypeSupported(type));
    if (!mimeType) { status('Recording format unavailable. Type your text instead.'); return; }
    status('Requesting microphone permission…');
    try {
      const granted = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (capture !== active || document.hidden) {
        granted.getTracks().forEach(track => track.stop());
        return;
      }
      capture.stream = granted;
      const recorder = new MediaRecorder(granted, { mimeType });
      capture.recorder = recorder;
      recorder.ondataavailable = event => { if (capture === active && event.data.size) capture.chunks.push(event.data); };
      granted.getTracks().forEach(track => { track.onended = () => { if (capture === active) cancel(); }; });
      recorder.onstop = () => {
        stopTracks(capture);
        if (capture !== active) return;
        const clip = new Blob(capture.chunks, { type: mimeType.split(';')[0] });
        capture.chunks = [];
        if (clip.size > 8 * 1024 * 1024 || !clip.size) {
          status('Recording is empty or too large. Type or record again.');
          return;
        }
        transcribe(clip, mimeType.split(';')[0], capture);
      };
      recorder.start();
      button.setAttribute('aria-pressed', 'true');
      status('Recording… release or tap again to stop. Maximum 60 seconds.');
      capture.timer = setTimeout(() => { if (capture === active) stop(); }, 60000);
    } catch (_) {
      stopTracks(capture);
      if (capture === active) {
        active = null;
        status('Microphone unavailable or denied. Type or record again.');
      }
    }
  }

  function stop() {
    const capture = active;
    if (!capture || !capture.recorder || capture.recorder.state === 'inactive') return;
    clearTimeout(capture.timer);
    micButtons.forEach(button => button.setAttribute('aria-pressed', 'false'));
    status('Transcribing on the Teem server…');
    capture.recorder.stop();
  }

  async function transcribe(clip, mimeType, capture) {
    const controller = new AbortController();
    capture.upload = controller;
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch('/transcribe', { method: 'POST', body: clip,
        headers: { 'Content-Type': mimeType }, signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error('recognition failed');
      const result = await response.json();
      if (capture !== active || Number(capture.field.dataset.editVersion || 0) !== capture.version) return;
      if (typeof result.text !== 'string' || !result.text.trim()) throw new Error('empty transcription');
      capture.field.value = result.text;
      capture.field.dataset.editVersion = String(capture.version + 1);
      status('Transcription ready. Check both fields and edit any errors before proposing.');
    } catch (_) {
      if (capture === active) status('Transcription failed. Type or make a fresh recording.');
    } finally {
      clearTimeout(timeout);
      if (capture.upload === controller) capture.upload = null;
    }
  }

  ['objective', 'criteria'].forEach(id => {
    const target = document.getElementById(id);
    if (target) target.addEventListener('input', () => {
      target.dataset.editVersion = String(Number(target.dataset.editVersion || 0) + 1);
    });
  });
  for (const button of micButtons) {
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('pointerdown', event => {
      if (event.pointerType === 'mouse' || event.pointerType === 'touch' || event.pointerType === 'pen') {
        pressedPointer = true;
        pressStarted = performance.now();
        button.setPointerCapture(event.pointerId);
        if (active?.recorder?.state === 'recording') { pressStarted = Infinity; stop(); } else start(button);
      }
    });
    button.addEventListener('pointerup', () => {
      if (pressedPointer && performance.now() - pressStarted >= 350) {
        if (active?.recorder?.state === 'recording') stop(); else cancel();
      }
    });
    button.addEventListener('pointercancel', cancel);
    button.addEventListener('click', () => {
      if (pressedPointer) { pressedPointer = false; return; }
      if (active?.recorder?.state === 'recording') stop(); else start(button);
    });
  }
  addEventListener('pagehide', cancel);
  document.addEventListener('visibilitychange', () => { if (document.hidden) cancel(); });

  const pushStatus = document.getElementById('push-status');
  const enable = document.getElementById('enable-push');
  const disable = document.getElementById('disable-push');
  const storedId = () => localStorage.getItem('teemPushId');
  if (pushStatus) pushStatus.textContent = storedId() ? 'Enabled for this install' : 'Disabled';
  if (enable) enable.addEventListener('click', async () => {
    try {
      if (!('PushManager' in window) || !('Notification' in window)) throw new Error();
      if (await Notification.requestPermission() !== 'granted') {
        pushStatus.textContent = 'Notifications denied. Foreground status remains available.';
        return;
      }
      const configResponse = await fetch('/config', { cache: 'no-store' });
      if (!configResponse.ok) throw new Error();
      const config = await configResponse.json();
      if (!config.vapid_public_key) throw new Error();
      const registration = await navigator.serviceWorker.ready;
      const key = config.vapid_public_key.replace(/-/g, '+').replace(/_/g, '/');
      const bytes = Uint8Array.from(atob(key.padEnd(Math.ceil(key.length / 4) * 4, '=')), char => char.charCodeAt(0));
      const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytes });
      const body = { ...subscription.toJSON(), previous_id: storedId() || undefined };
      const response = await fetch('/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), cache: 'no-store' });
      if (!response.ok) throw new Error();
      localStorage.setItem('teemPushId', (await response.json()).id);
      pushStatus.textContent = 'Enabled for this install';
    } catch (_) { pushStatus.textContent = 'Notifications unavailable. Foreground status remains available.'; }
  });
  if (disable) disable.addEventListener('click', async () => {
    try {
      const id = storedId();
      if (id) {
        const response = await fetch('/push/unsubscribe', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }), cache: 'no-store' });
        if (!response.ok) throw new Error();
      }
      const subscription = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
      if (subscription) await subscription.unsubscribe();
      localStorage.removeItem('teemPushId');
      pushStatus.textContent = 'Disabled';
    } catch (_) { pushStatus.textContent = 'Could not disable. Try again when connected.'; }
  });
})();
