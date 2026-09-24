const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const script = fs.readFileSync(require.resolve('../teem/static/phone.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(permission, transcription, deferredStop = false) {
  const handlers = {};
  const documentHandlers = {};
  const buttonHandlers = {};
  const streams = [];
  const field = { value: '', dataset: {}, addEventListener(name, fn) { this[name] = fn; } };
  const status = { textContent: '' };
  const button = { dataset: { field: 'objective' }, setAttribute() {}, setPointerCapture() {},
    addEventListener(name, fn) { buttonHandlers[name] = fn; } };
  const document = {
    hidden: false,
    getElementById(id) { return id === 'objective' ? field : id === 'recording-status' ? status : null; },
    querySelectorAll(selector) { return selector === '.mic' ? [button] : []; },
    addEventListener(name, fn) { documentHandlers[name] = fn; }
  };
  const recorders = [];
  let uploads = 0;
  const uploadedClips = [];
  class Recorder {
    constructor() { this.state = 'inactive'; recorders.push(this); }
    static isTypeSupported() { return true; }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      if (!deferredStop) this.finish();
    }
    finish() {
      this.ondataavailable({ data: new Blob([`audio-${recorders.indexOf(this) + 1}`]) });
      this.onstop();
    }
  }
  const context = {
    document, navigator: { mediaDevices: { getUserMedia: () => {
      const track = { stopped: false, stop() { this.stopped = true; } };
      const stream = { getTracks() { return [track]; } };
      streams.push({ track, stream });
      return permission(stream);
    } } },
    MediaRecorder: Recorder, Blob, AbortController, performance,
    location: { pathname: '/' },
    fetch: (path, options) => {
      if (path === '/state') return Promise.resolve({ ok: true, json: async () => ({ refreshed_at: new Date().toISOString(), runs: [] }) });
      uploads++;
      uploadedClips.push(options.body);
      return transcription;
    },
    setInterval() {}, setTimeout() { return 1; }, clearTimeout() {},
    addEventListener(name, fn) { handlers[name] = fn; }
  };
  vm.runInNewContext(script, context);
  return { buttonHandlers, documentHandlers, handlers, document, streams, recorders, uploadedClips, field, status,
    get track() { return streams[0]?.track; }, get recorder() { return recorders.at(-1); }, get uploads() { return uploads; } };
}

(async () => {
  let grant;
  const late = harness(() => new Promise(resolve => { grant = resolve; }));
  late.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  late.document.hidden = true;
  late.documentHandlers.visibilitychange();
  grant({ getTracks: () => [late.track] });
  await tick();
  assert.equal(late.track.stopped, true);
  assert.equal(late.recorder, undefined);
  assert.equal(late.uploads, 0);

  const interrupted = harness(async stream => stream);
  interrupted.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  await tick();
  assert.equal(interrupted.recorder.state, 'recording');
  interrupted.track.onended();
  assert.equal(interrupted.track.stopped, true);
  assert.equal(interrupted.uploads, 0);

  let finish;
  const superseded = harness(async stream => stream, new Promise(resolve => { finish = resolve; }));
  superseded.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  await tick();
  superseded.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  assert.equal(superseded.uploads, 1);
  superseded.field.value = 'my correction';
  superseded.field.input();
  finish({ ok: true, json: async () => ({ text: 'wrong transcript' }) });
  await tick();
  assert.equal(superseded.field.value, 'my correction');

  const delayed = harness(async stream => stream,
    Promise.resolve({ ok: true, json: async () => ({ text: 'second recording' }) }), true);
  delayed.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  await tick();
  delayed.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  delayed.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  await tick();
  delayed.recorders[0].finish();
  assert.equal(delayed.streams[1].track.stopped, false);
  delayed.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  delayed.recorders[1].finish();
  await tick();
  assert.equal(delayed.uploads, 1);
  assert.equal(await delayed.uploadedClips[0].text(), 'audio-2');
  assert.equal(delayed.field.value, 'second recording');

  const cancelled = harness(async stream => stream, undefined, true);
  cancelled.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  await tick();
  cancelled.buttonHandlers.pointercancel();
  cancelled.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  await tick();
  cancelled.recorders[0].finish();
  assert.equal(cancelled.streams[1].track.stopped, false);
  assert.equal(cancelled.uploads, 0);

  let rejectOld;
  const stalePermission = harness(stream => stalePermission.streams.length === 1 ?
    new Promise((_, reject) => { rejectOld = reject; }) : Promise.resolve(stream));
  stalePermission.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  stalePermission.buttonHandlers.pointerdown({ pointerType: 'touch', pointerId: 1 });
  await tick();
  rejectOld(new Error('permission rejected'));
  await tick();
  assert.equal(stalePermission.streams[1].track.stopped, false);
  assert.equal(stalePermission.recorder.state, 'recording');
})().catch(error => { console.error(error); process.exitCode = 1; });
