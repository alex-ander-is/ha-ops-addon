/* Execute the complete frontend with inert Lit imports. These are logic contracts,
 * not DOM, Vaadin focus, paint, or node-identity evidence; see preview-decisions.mjs. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const sourcePath = process.env.HA_OPS_FRONTEND_SOURCE || path.resolve(__dirname, '../../frontend/src/ha-ops.js');
const registered = new Map();
const timers = new Map();
let timerId = 0;
const context = {
  LitElement: class { requestUpdate() {} disconnectedCallback() {} },
  css: () => '', html: (strings, ...values) => ({ strings, values }), nothing: Symbol('nothing'),
  customElements: { define: (name, klass) => registered.set(name, klass) },
  document: { activeElement: null, body: {}, removeEventListener() {} },
  window: { WebSocket: { OPEN: 1, CLOSED: 3 }, location: { href: 'http://localhost/' } },
  location: { href: 'http://localhost/' }, console, crypto: crypto.webcrypto,
  TextEncoder, URL, structuredClone, CustomEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
  requestAnimationFrame: callback => callback(),
  setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
  clearTimeout: id => timers.delete(id),
};
const source = fs.readFileSync(sourcePath, 'utf8').replace(/^import .*;\n/gm, '');
vm.runInNewContext(source, context, { filename: sourcePath });
const App = registered.get('ha-ops-app');
const Preview = registered.get('ha-ops-preview');
const File = registered.get('ha-ops-preview-file');
const commands = ['select_apply_preview', 'resolve_apply_preview', 'select_save_preview', 'resolve_save_preview'];
let checks = 0;
function fixture() {
  const a = new App(); a.connection = 'http'; a.replayPending = false;
  a.state = { operation_generation: 7, last_status: 'success', command_records: {},
    apply_preview_id: 'apply-content', save_preview_id: 'save-content',
    apply_decision_revision: 3, save_decision_revision: 5,
    last_preview_paths: ['a.yaml', 'b.yaml'], last_save_preview_paths: ['a.yaml', 'b.yaml'],
    last_preview_commit: 'commit-a', last_save_preview_commit: 'commit-s',
    last_preview_fingerprint: 'fingerprint-a', last_save_preview_fingerprint: 'fingerprint-s',
    last_diff_cursor: { schema: 1, kind: 'apply', generation: 7, artifact: 'a', sha256: 'aa', bytes: 10 },
    last_save_diff_cursor: { schema: 1, kind: 'save', generation: 7, artifact: 's', sha256: 'ss', bytes: 12 },
    apply_preview_selected_paths: ['a.yaml'], save_preview_selected_paths: ['a.yaml'],
    apply_preview_resolutions: {}, save_preview_resolutions: {}, last_save_commit_subject: 'default subject' };
  a.view = { display_times: {} }; return a;
}
function shown(a) { return a.previewTemplate() !== context.nothing; }
function start(a, command, transport) {
  let finish, fail; const sent = [];
  if (transport === 'ws') a.socket = { readyState: 1, send: raw => sent.push(JSON.parse(raw)), close() {} };
  context.fetch = (url, options) => {
    assert.equal(options?.method, 'POST'); sent.push(JSON.parse(options.body));
    return new Promise((resolve, reject) => { finish = resolve; fail = reject; });
  };
  a.pollHttpCommand = async () => {};
  const result = a.dispatchCommand(command, 'decision', { preview_identity: { decision_revision: 3 } }).then(() => 'ok', error => error.message);
  const envelope = sent[0]; assert(envelope, 'first command must send');
  const intent = a.commandIntent;
  return { a, command, intent, id: envelope.command_id, result, sent,
    complete(outcome) {
      if (transport === 'ws') {
        if (outcome === 'ambiguous') { const [id, entry] = [...a.pending.entries()][0]; a.pending.delete(id); entry.reject(new Error('transport failed')); }
        else a.receive({ type: 'result', id: envelope.id, ok: outcome === 'success', message: 'explicit rejection' });
      } else if (outcome === 'ambiguous') fail(new Error('transport failed'));
      else finish({ ok: outcome === 'success', json: async () => ({ ok: outcome === 'success', message: 'explicit rejection' }) });
    }
  };
}
function terminal(r) {
  r.a.state = { ...r.a.state, command_records: { [r.id]: { command: r.command, status: 'terminal' } } };
  r.a.reconcileAcceptedCommand();
}
async function continuity() {
  const replayed = fixture(); replayed.state.command_records.x = { command: commands[0], status: 'running' };
  assert(shown(replayed), 'authoritative running checkbox decision must retain preview content'); checks++;
  for (const command of commands) for (const transport of ['ws', 'http']) {
    const r = start(fixture(), command, transport);
    assert(shown(r.a), `${command}/${transport}: pending decision hides content`);
    assert(r.a.mutationBlocked());
    await r.a.dispatchCommand(commands[1], 'decision'); await r.a.dispatchCommand('apply', 'apply');
    assert.equal(r.sent.length, 1, 'synchronous second decisions and Confirm must be fenced');
    r.complete('success'); await r.result;
    assert(shown(r.a), `${command}/${transport}: acceptance before record hides content`);
    assert(r.a.mutationBlocked());
    for (const status of ['accepted', 'running', 'failed_unknown']) {
      r.a.state.command_records[r.id] = { command, status }; r.a.reconcileAcceptedCommand();
      assert(shown(r.a), `${command}/${transport}/${status}: decision hides content`); assert(r.a.mutationBlocked());
    }
    terminal(r); assert(!r.a.mutationBlocked()); assert(shown(r.a)); checks++;
  }
}
async function fences() {
  for (const command of commands) for (const status of ['accepted', 'running', 'failed_unknown']) {
    const a = fixture(); a.state.command_records.x = { command, status }; assert(shown(a)); assert(a.mutationBlocked()); checks++;
  }
  for (const gate of ['missing-type', 'unknown', 'mixed', 'operation', 'running-action', 'recovery', 'prune', 'replay', 'disconnected']) {
    const a = fixture(); a.state.command_records.x = { command: commands[0], status: 'terminal' };
    if (gate === 'missing-type') a.acceptedCommandId = 'not-recorded';
    if (gate === 'unknown') a.state.command_records.y = { command: 'unknown', status: 'accepted' };
    if (gate === 'mixed') a.state.command_records = { x: { command: commands[0], status: 'running' }, y: { command: 'save', status: 'running' } };
    if (gate === 'operation') a.state.active_operation = { command: 'apply' };
    if (gate === 'running-action') { a.state.last_status = 'running'; a.state.last_action = 'unknown'; }
    if (gate === 'recovery') a.state.deleted_devices_recovery_phase = 'restore_required';
    if (gate === 'prune') a.state.docker_build_cache_prune_fence = true;
    if (gate === 'replay') a.replayPending = true;
    if (gate === 'disconnected') a.connection = 'unknown';
    assert(a.mutationBlocked(), gate); if (!['recovery', 'prune', 'replay', 'disconnected'].includes(gate)) assert(!shown(a), gate);
    checks++;
  }
}
async function detachedRejection() {
  for (const command of commands) for (const transport of ['ws', 'http']) for (const boundary of ['unknown', 'version', 'content', 'record-cleared', 'detach']) {
    const r = start(fixture(), command, transport);
    if (boundary === 'unknown') r.a.markUnknown(new Error('poll failed'));
    if (boundary === 'version') { r.a.backendVersion = '1.1.1'; r.a.clientVersion = '1.1.1'; r.a.observeBackendVersion('1.1.2'); }
    if (boundary === 'content') { r.a.state.operation_generation++; r.a.reconcileAcceptedCommand(); }
    if (boundary === 'record-cleared') { r.a.state.command_records[r.id] = { command, status: 'running' }; r.a.reconcileAcceptedCommand(); delete r.a.state.command_records[r.id]; r.a.reconcileAcceptedCommand(); }
    if (boundary === 'detach') r.a.disconnectedCallback();
    r.a.uncertainCommandId = r.id; r.complete('rejected'); assert.equal(await r.result, 'explicit rejection');
    assert.equal(r.a.acceptedCommandId, null, boundary); assert.equal(r.a.uncertainCommandId, null, boundary);
    assert.equal(r.a.commandIntent, null); assert.equal(r.a.decisionDisplay, null); assert.equal(r.a.decisionFocus, null);
    r.a.replayPending = false; r.a.connection = 'http'; assert(!r.a.mutationBlocked()); assert.equal(r.sent.length, 1); checks++;
  }
  for (const transport of ['ws', 'http']) for (const outcome of ['rejected', 'success', 'ambiguous']) {
    const r = start(fixture(), commands[0], transport); r.a.markUnknown(new Error('poll failed'));
    const newer = { id: 'new', command: commands[2], sent: true, direction: 'save', contentKey: vm.runInNewContext('previewContentKey', context)(r.a.state, 'save') };
    const focus = { commandId: 'new', settled: false }; r.a.commandIntent = newer; r.a.decisionFocus = focus;
    r.a.acceptedCommandId = 'new'; r.a.uncertainCommandId = 'new'; r.a.decisionDisplay = { ...newer };
    r.complete(outcome); await r.result; assert.equal(r.a.commandIntent, newer); assert.equal(r.a.acceptedCommandId, 'new');
    assert.equal(r.a.uncertainCommandId, 'new'); assert.equal(r.a.decisionFocus, focus); assert(!focus.settled); checks++;
  }
  for (const gate of ['record', 'operation', 'recovery', 'prune', 'replay']) {
    const r = start(fixture(), commands[0], 'http'); r.a.markUnknown(new Error('poll failed'));
    if (gate === 'record') r.a.state.command_records[r.id] = { command: commands[0], status: 'running' };
    if (gate === 'operation') r.a.state.active_operation = { command: 'apply' };
    if (gate === 'recovery') r.a.state.deleted_devices_recovery_phase = 'failed';
    if (gate === 'prune') r.a.state.docker_build_cache_prune_fence = true;
    if (gate === 'replay') r.a.replayPending = true;
    r.complete('rejected'); await r.result; r.a.connection = 'http'; assert(r.a.mutationBlocked(), gate); checks++;
  }
}
async function uncertain() {
  for (const command of commands) for (const transport of ['ws', 'http']) {
    const r = start(fixture(), command, transport); r.a.markUnknown(new Error('connection lost before record'));
    assert(shown(r.a), 'known decision must retain content after disconnect before record'); assert(r.a.mutationBlocked());
    r.complete('ambiguous'); await r.result; r.a.connection = 'http'; assert(shown(r.a)); assert(r.a.mutationBlocked());
    await r.a.dispatchCommand(command, 'decision'); assert.equal(r.sent.length, 1);
    terminal(r); assert(!r.a.mutationBlocked()); assert.equal(r.a.decisionDisplay, null); assert.equal(r.a.decisionFocus, null); checks++;
  }
  class Socket {
    static OPEN = 1; static CLOSED = 3;
    constructor() { this.readyState = 1; this.handlers = {}; this.sent = []; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    close() { this.readyState = 3; this.handlers.close(); }
  }
  context.WebSocket = Socket; context.window.WebSocket = Socket;
  const a = fixture(); a.shouldReconnect = true; a.connect(); const socket = a.socket;
  socket.handlers.open(); a.receive({ type: 'ready', schema_version: 1, revision: 1, state: a.state });
  const result = a.dispatchCommand(commands[0], 'decision').catch(error => error.message); const id = a.commandIntent.id; const frame = socket.sent.at(-1);
  socket.close(); assert.equal(await result, 'Command outcome is unknown after disconnect.'); assert.equal(a.pending.size, 0);
  a.receive({ type: 'result', id: frame.id, ok: false, message: 'late refusal' }); a.connection = 'http'; a.replayPending = false;
  assert(a.mutationBlocked()); assert(shown(a));
  a.receive({ type: 'replay', schema_version: 1, revision: 2, state: { ...a.state, command_records: { [id]: { command: commands[0], status: 'terminal' } } } });
  assert(!a.mutationBlocked()); assert.equal(a.decisionDisplay, null); checks++;
}
async function content() {
  for (const direction of ['apply', 'save']) {
    const p = new Preview(); p.direction = direction; p.state = fixture().state; p.willUpdate();
    p.wrapByPath = { 'a.yaml': false }; p.commitSubject = 'edited subject';
    p.state = { ...p.state, [`${direction}_decision_revision`]: 99 }; p.willUpdate();
    assert.equal(p.wrapByPath['a.yaml'], false); if (direction === 'save') assert.equal(p.commitSubject, 'edited subject');
    p.state = { ...p.state, operation_generation: 8 }; p.willUpdate(); assert.equal(Object.keys(p.wrapByPath).length, 0);
    if (direction === 'save') assert.equal(p.commitSubject, 'default subject'); checks++;
  }
  for (const field of ['schema', 'kind', 'generation', 'artifact', 'sha256', 'bytes', 'path', 'contentKey']) for (const outcome of ['success', 'error']) {
    const f = new File(); f.path = 'a.yaml'; f.cursor = fixture().state.last_diff_cursor; f.generation = 7; f.contentKey = 'current'; f.isConnected = true;
    let finish, fail; context.fetch = () => new Promise((resolve, reject) => { finish = resolve; fail = reject; });
    const promise = f.setExpanded(true); const changed = new Map();
    if (field === 'path' || field === 'contentKey') { changed.set(field, f[field]); f[field] = 'replacement'; }
    else { changed.set('cursor', f.cursor); f.cursor = { ...f.cursor, [field]: `replacement-${field}` }; }
    f.willUpdate(changed);
    if (outcome === 'success') finish({ json: async () => ({ ok: true, diff: 'STALE', semantic: { stale: true } }) }); else fail(new Error('old error'));
    await promise; assert(!f.expanded); assert.equal(f.diff, ''); assert.equal(f.semantic, null); assert.equal(f.diffState, 'idle'); checks++;
  }
  const f = new File(); f.path = 'a.yaml'; f.cursor = fixture().state.last_diff_cursor; f.generation = 7; f.contentKey = 'current'; f.isConnected = true;
  let finish; let requests = 0; context.fetch = () => { requests++; return new Promise(resolve => finish = resolve); };
  const promise = f.setExpanded(true); f.selected = true; f.willUpdate(new Map([['selected', false]]));
  finish({ json: async () => ({ ok: true, diff: 'current diff', semantic: { counts: {} } }) }); await promise;
  assert.equal(f.diff, 'current diff'); assert(f.expanded); await f.setExpanded(true); assert.equal(requests, 1); checks++;
}
async function authority() {
  // Vaadin's light-DOM input is event.target, while currentTarget is the
  // controlled checkbox host. Its checked setter delegates to the native input.
  for (const authoritative of [false, true]) {
    const input = { checked: !authoritative }; let hostChecked = !authoritative;
    const host = { get checked() { return hostChecked; }, set checked(value) { hostChecked = value; input.checked = value; } };
    const f = new File(); f.path = 'a.yaml'; f.selected = authoritative;
    let requested; f.dispatchEvent = event => requested = event.detail.selected;
    f.onSelectChange({ target: input, currentTarget: host });
    assert.equal(requested, !authoritative, 'must emit the native requested selection');
    assert.equal(host.checked, authoritative, 'must reset Vaadin host to authoritative selection');
    assert.equal(input.checked, authoritative, 'host must delegate reset to native input');
    // Reconciliation updates the host, which delegates to the same native input.
    host.checked = requested; assert.equal(input.checked, requested); checks++;
  }
  for (const direction of ['apply', 'save']) {
    const p = new Preview(); p.direction = direction; p.state = fixture().state; const events = [];
    p.dispatchEvent = event => events.push(event.detail); const event = detail => ({ detail, stopPropagation() {} });
    p.onPreviewSelect(event({ path: 'b.yaml', selected: true })); p.onPreviewSelect(event({ path: 'a.yaml', selected: false }));
    p.selectAll(true); p.selectAll(false); p.onPreviewResolve(event({ path: 'a.yaml', choice: 'ha' })); p.onPreviewResolve(event({ path: 'a.yaml', choice: 'git' }));
    assert.equal(events.length, 6); for (const e of events) { assert.equal(e.payload.preview_identity.direction, direction); assert.equal(e.payload.preview_identity.decision_revision, direction === 'save' ? 5 : 3); assert.equal(e.payload.preview_identity.diff_cursor.generation, 7); }
    p.state = { ...p.state, last_save_preview_conflict_paths: ['a.yaml'] };
    if (direction === 'save') { assert(p.isFinalActionDisabled()); p.state.save_preview_resolutions = { 'a.yaml': 'ha' }; }
    assert(!p.isFinalActionDisabled()); p.running = true; assert(p.isFinalActionDisabled()); p.selectAll(true); assert.equal(events.length, 6); checks++;
  }
}
async function focus() {
  // Inert connected elements prove cancellation/authority logic, not native blur.
  for (const gate of ['pending', 'accepted', 'uncertain', 'replay', 'operation', 'disabled', 'replaced', 'removed', 'eligible', 'pointer', 'focus-other']) {
    const a = fixture(); let focused = 0;
    const preview = { localName: 'ha-ops-preview', direction: 'apply', isConnected: true, updateComplete: Promise.resolve() };
    const control = { localName: 'vaadin-checkbox', parentElement: preview, isConnected: true, disabled: false };
    const target = { isConnected: true, focus(options) { assert(options.preventScroll); focused++; } };
    a.decisionFocus = { commandId: 'x', control, target, preview, row: null, rowKey: null,
      contentKey: vm.runInNewContext('previewContentKey', context)(a.state, 'apply'), settled: true, restoring: false };
    if (gate === 'pending') a.decisionFocus.settled = false;
    if (gate === 'accepted') a.acceptedCommandId = 'x'; if (gate === 'uncertain') a.uncertainCommandId = 'x';
    if (gate === 'replay') a.replayPending = true; if (gate === 'operation') a.state.active_operation = {};
    if (gate === 'disabled') control.disabled = true; if (gate === 'removed') target.isConnected = false;
    if (gate === 'replaced') a.state.apply_preview_id = 'replacement';
    if (gate === 'pointer') a.cancelDecisionFocus();
    if (gate === 'focus-other') a.onDecisionFocusIn({ composedPath: () => [], target: {} });
    await a.restoreDecisionFocus(); assert.equal(focused, gate === 'eligible' ? 1 : 0, gate); checks++;
  }
}
const groups = { continuity, fences, detachedRejection, uncertain, content, authority, focus };
(async () => {
  const name = process.argv[2]; assert(groups[name], `unknown scenario: ${name}`); await groups[name]();
  console.log(JSON.stringify({ scenario: name, checks, completeProductionModule: true, rendered: false, result: 'passed' }));
})().catch(error => { console.error(error); process.exitCode = 1; });
