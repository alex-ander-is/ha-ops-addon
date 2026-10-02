/* Visible regression layer. Run through the shared playwright-node wrapper.
 * HA_OPS_BROWSER_BUNDLE_OVERRIDE serves a disposable historical bundle for
 * baseline proof without resetting the repository or writing product files. */
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, '../..');
const sharedRoot = '/Users/purportex/Applications/Playwright';
const { chromium } = await import(pathToFileURL(path.join(sharedRoot, 'src/runtime.mjs')).href);
const artifacts = process.env.HA_OPS_BROWSER_ARTIFACTS_DIR || `/private/tmp/ha-ops-preview-decisions-${Date.now()}`;
mkdirSync(artifacts, { recursive: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
const child = spawn('python3', [path.join(appRoot, 'dev_harness.py'), '--port', '0', '--print-json'], {
  cwd: path.dirname(appRoot), stdio: ['ignore', 'pipe', 'inherit'],
});
const { baseUrl } = await new Promise((resolve, reject) => {
  let output = '';
  child.stdout.on('data', data => { output += data; const line = output.split('\n').find(line => line.startsWith('{')); if (line) resolve(JSON.parse(line)); });
  child.once('exit', code => reject(new Error(`harness exited ${code}`)));
});
// Preserve the shared context and all existing user pages. No anonymous context.
const existing = process.env.HA_OPS_BROWSER_CDP_URL ? await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL) : null;
const context = existing ? existing.contexts()[0] : await chromium.launchPersistentContext(path.join(sharedRoot, 'user-data/google-chrome'), {
  channel: 'chrome', headless: false, viewport: { width: 1600, height: 1000 },
  args: ['--profile-directory=Default', '--remote-debugging-port=9227'],
});
const pages = context.pages();
const page = pages.find(page => page.url() === 'about:blank') || pages.find(page => page.url().startsWith(baseUrl)) || await context.newPage();
const bundleOverride = process.env.HA_OPS_BROWSER_BUNDLE_OVERRIDE;
const probeMode = process.env.HA_OPS_BROWSER_PROBE || 'full';
assert(['full', 'checkbox-baseline'].includes(probeMode), `unknown probe mode: ${probeMode}`);
if (bundleOverride) await page.route('**/assets/ha-ops.js', route => route.fulfill({ contentType: 'application/javascript', body: readFileSync(bundleOverride) }));
const errors = [];
page.on('pageerror', error => errors.push(error.message));
let diffRequests = 0, navigations = 0;
page.on('request', request => { if (request.url().includes('/diff-get?')) diffRequests++; });
page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
const results = [], realEnvelopes = [];
page.on('websocket', socket => socket.on('framesent', ({ payload }) => {
  try { const e = JSON.parse(payload); if (/^(select|resolve)_(apply|save)_preview$/.test(e.command)) realEnvelopes.push(e); } catch {}
}));
page.on('request', request => {
  if (request.method() === 'POST') {
    try { const e = JSON.parse(request.postData()); if (/^(select|resolve)_(apply|save)_preview$/.test(e.command)) realEnvelopes.push(e); } catch {}
  }
});
const twoFrames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const settle = () => page.waitForFunction(() => { const app = document.querySelector('ha-ops-app'); return app && !app.mutationBlocked(); });
async function waitForRows(direction) {
  await page.waitForFunction(direction => {
    const a = document.querySelector('ha-ops-app'), p = a?.querySelector(`ha-ops-preview[direction="${direction}"]`);
    const paths = a?.state[direction === 'save' ? 'last_save_preview_paths' : 'last_preview_paths'];
    const cursor = a?.state[direction === 'save' ? 'last_save_diff_cursor' : 'last_diff_cursor'];
    if (!p || !Array.isArray(paths) || paths.length !== 2 || !cursor
      || Number(cursor.generation) !== Number(a.state.operation_generation)) return false;
    const rows = [...p.renderRoot.querySelectorAll('ha-ops-preview-file')];
    return rows.length === paths.length && paths.every(path => rows.some(r => r.path === path));
  }, direction);
  await page.locator('ha-ops-preview').evaluateAll(async previews => {
    for (const p of previews) {
      await p.updateComplete;
      await Promise.all([...p.renderRoot.querySelectorAll('ha-ops-preview-file')].map(r => r.updateComplete));
    }
  });
}
async function expandLoaded(direction) {
  await waitForRows(direction);
  await page.locator('ha-ops-preview').evaluateAll(async previews => {
    for (const p of previews) for (const r of p.renderRoot.querySelectorAll('ha-ops-preview-file')) await r.setExpanded(true);
  });
  await page.waitForFunction(direction => {
    const a = document.querySelector('ha-ops-app'), p = a.querySelector(`ha-ops-preview[direction="${direction}"]`);
    const paths = p.paths, rows = [...p.renderRoot.querySelectorAll('ha-ops-preview-file')];
    if (!paths.length || rows.length !== paths.length) return false;
    const mounted = [...a.querySelectorAll('ha-ops-preview')].flatMap(p => [...p.renderRoot.querySelectorAll('ha-ops-preview-file')]);
    if (mounted.some(r => r.diffState === 'stale')) throw new Error('genuine diff loading became stale');
    return mounted.length > 0 && mounted.every(r => r.expanded && r.diffState === 'loaded'
      && r.diff.length > 0 && r.renderRoot.querySelector('pre')?.isConnected);
  }, direction);
}
const sameSet = (a, b) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
async function authority(direction) {
  return page.locator(`ha-ops-preview[direction="${direction}"]`).evaluate(p => ({
    paths: [...p.paths], selected: [...(p.state[`${p.direction}_preview_selected_paths`] || [])],
    choices: { ...(p.state[`${p.direction}_preview_resolutions`] || {}) },
    rows: [...p.renderRoot.querySelectorAll('ha-ops-preview-file')].map(r => ({ path: r.path,
      selected: r.selected, checked: r.renderRoot.querySelector('vaadin-checkbox').checked, nativeChecked: r.renderRoot.querySelector('vaadin-checkbox').inputElement.checked, choice: r.choice })),
  }));
}
function expectedAfter(before, action, rowPath) {
  const selected = new Set(before.selected), choices = { ...before.choices };
  if (action === 'all' || action === 'none') { selected.clear(); if (action === 'all') before.paths.forEach(p => selected.add(p)); }
  else if (action === 'check') selected.add(rowPath);
  else if (action === 'uncheck') selected.delete(rowPath);
  else choices[rowPath] = action;
  return { ...before, selected: [...selected], choices };
}
async function assertAuthority(direction, expected, label) {
  const actual = await authority(direction), fallback = direction === 'save' ? 'ha' : 'git';
  assert(actual.rows.length === expected.paths.length && sameSet(actual.paths, expected.paths)
    && sameSet(actual.selected, expected.selected) && JSON.stringify(actual.choices) === JSON.stringify(expected.choices)
    && actual.rows.every(r => r.selected === expected.selected.includes(r.path) && r.checked === r.selected && r.nativeChecked === r.selected
      && r.choice === (expected.choices[r.path] || fallback)), `${label}: selection/choice authority ${JSON.stringify({ expected, actual })}`);
  return actual;
}
// Each real preview advances operation_generation and invalidates the other.
async function prepare(transport, direction, mode = 'ordinary') {
  assert(['ordinary', 'delayed'].includes(mode), 'explicit fixture mode required');
  if (transport === 'http') await page.addInitScript(() => { window.WebSocket = undefined; });
  await page.goto(baseUrl); await settle();
  await page.getByRole('button', { name: direction === 'apply' ? 'Preview Git to HA' : 'Preview HA to Git', exact: true }).click();
  await settle();
  await waitForRows(direction);
  let seededIdentity = null;
  if (mode === 'delayed') {
    // Pause transport before the test-only content identity boundary. Every
    // subsequent decision is captured locally; ordinary backend traffic is real.
    await installDelay(transport);
    seededIdentity = await page.locator('ha-ops-app').evaluate(async (a, direction) => {
      const id = `browser-delayed-${direction}-${a.state.operation_generation}`;
      a.state = { ...a.state, [`${direction}_preview_id`]: id };
      await a.updateComplete;
      for (const p of a.querySelectorAll('ha-ops-preview')) {
        await p.updateComplete;
        await Promise.all([...p.renderRoot.querySelectorAll('ha-ops-preview-file')].map(r => r.updateComplete));
      }
      return id;
    }, direction);
  }
  await expandLoaded(direction);
  const overflow = await page.evaluate(async direction => {
    const app = document.querySelector('ha-ops-app');
    for (const preview of app.querySelectorAll('ha-ops-preview')) {
      const rows = [...preview.renderRoot.querySelectorAll('ha-ops-preview-file')];
      preview.wrapByPath = Object.fromEntries(rows.map(row => [row.path, false]));
      if (preview.direction === 'save') preview.commitSubject = 'Keep my reviewed subject';
      await preview.updateComplete;
      for (const [index, row] of rows.entries()) {
        // Disposable long/wide content goes through the real row template, not
        // an invented scroll container. Semantic and raw templates both run.
        row.diff = Array.from({ length: 48 }, (_, n) => `+ line ${n} ${'wide-review-content '.repeat(24)}`).join('\n');
        row.semantic = index === 0 ? { counts: { added: 1, removed: 0, changed: 0 },
          rows: [{ kind: 'added', label: 'fixture entity', fields: ['name'], review: true }] } : null;
        await row.updateComplete;
        const nested = row.renderRoot.querySelector('.raw-registry-diff');
        if (nested) { nested.opened = true; await nested.updateComplete; }
        const pre = row.renderRoot.querySelector('pre');
        if (!row.expanded || row.diffState !== 'loaded' || !pre?.isConnected) throw new Error(`missing current loaded pre: ${row.path}`);
        pre.style.maxHeight = '90px'; pre.style.maxWidth = '480px';
      }
    }
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const previews = [...app.querySelectorAll('ha-ops-preview')];
    const rows = previews.flatMap(preview => [...preview.renderRoot.querySelectorAll('ha-ops-preview-file')]);
    const nodes = rows.map(row => {
      const pre = row.renderRoot.querySelector('pre'); pre.scrollTop = 37; pre.scrollLeft = 53;
      return { row, diff: row.diff, semantic: row.semantic, detail: row.renderRoot.querySelector('vaadin-details'),
        semanticNode: row.renderRoot.querySelector('.registry-summary'), nested: row.renderRoot.querySelector('.raw-registry-diff'), pre,
        top: pre.scrollTop, left: pre.scrollLeft, height: pre.scrollHeight, clientHeight: pre.clientHeight,
        width: pre.scrollWidth, clientWidth: pre.clientWidth };
    });
    const tracked = [...previews, ...nodes.flatMap(n => [n.row, n.detail, n.semanticNode, n.nested, n.pre]).filter(Boolean)];
    const roots = new Set([app.querySelector('#reactive-previews')]);
    function shadowRoots(node) {
      if (node.shadowRoot) { roots.add(node.shadowRoot); for (const child of node.shadowRoot.querySelectorAll('*')) shadowRoots(child); }
    }
    for (const node of tracked) shadowRoots(node);
    // Shadow slots/content wrappers can be replaced while their light-DOM pre
    // stays connected. Retain those native detail structures as well.
    for (const root of roots) if (root.host?.localName === 'vaadin-details')
      tracked.push(...root.querySelectorAll('[part="content"], [part="summary"], slot'));
    const c = window.continuity = { direction, previews, rows, nodes, tracked, removals: [], frameFailures: [], frames: 0, observing: true };
    // Observe every relevant shadow tree. Removing and reinserting a row/pre
    // before the next frame is still a failure, even if final identity matches.
    c.observer = new MutationObserver(records => {
      for (const record of records) for (const node of record.removedNodes)
        for (const target of tracked) if (node === target || node.contains?.(target))
          c.removals.push({ removed: target.localName, path: target.path || '', root: record.target.getRootNode().host?.localName || 'app' });
    });
    for (const root of roots) c.observer.observe(root, { childList: true, subtree: true });
    function sample() {
      if (!c.observing) return;
      c.frames++;
      if (tracked.some(node => !node.isConnected) || nodes.some(n => !n.row.expanded || n.row.diffState !== 'loaded'
        || n.row.renderRoot.querySelector('pre') !== n.pre || n.row.renderRoot.querySelector('vaadin-details') !== n.detail
        || n.row.renderRoot.querySelector('.registry-summary') !== n.semanticNode || n.row.renderRoot.querySelector('.raw-registry-diff') !== n.nested))
        c.frameFailures.push(c.frames);
      requestAnimationFrame(sample);
    }
    requestAnimationFrame(sample);
    return { observedRoots: roots.size, semanticRows: nodes.filter(n => n.semanticNode).length, rawRows: nodes.filter(n => !n.semanticNode).length, requestedRows: nodes.filter(n => n.row.getRootNode().host.direction === direction).length, rows: nodes.map(({ top, left, height, clientHeight, width, clientWidth }) =>
      ({ top, left, height, clientHeight, width, clientWidth })) };
  }, direction);
  assert(overflow.requestedRows === 2 && overflow.semanticRows > 0 && overflow.rawRows > 0 && overflow.rows.length > 0 && overflow.rows.every(n => n.height > n.clientHeight && n.width > n.clientWidth
    && n.top > 0 && n.left > 0), `scroll fixture must overflow both axes: ${JSON.stringify(overflow)}`);
  results.push({ transport, direction, mode, seededIdentity, overflow });
}
async function retained(label) {
  await twoFrames();
  const evidence = await page.evaluate(() => {
    const c = window.continuity, app = document.querySelector('ha-ops-app');
    return { samePreviews: c.previews.every(preview => [...app.querySelectorAll('ha-ops-preview')].includes(preview)),
      removals: c.removals, frames: c.frames, frameFailures: c.frameFailures,
      rows: c.nodes.map(({ row, diff, semantic, detail, semanticNode, nested, pre, top, left }) => ({ connected: row.isConnected, expanded: row.expanded,
        loaded: row.diffState === 'loaded', diffSame: row.diff === diff, semanticSame: row.semantic === semantic,
        wrap: row.wrapLines, preSame: row.renderRoot.querySelector('pre') === pre,
        detailSame: row.renderRoot.querySelector('vaadin-details') === detail, detailOpen: detail.opened,
        semanticNodeSame: row.renderRoot.querySelector('.registry-summary') === semanticNode,
        scrollSame: pre.scrollTop === top && pre.scrollLeft === left, top: pre.scrollTop, left: pre.scrollLeft,
        nestedSame: row.renderRoot.querySelector('.raw-registry-diff') === nested, nestedOpen: !nested || nested.opened })),
      requestedDirection: c.direction, hasSave: c.previews.some(p => p.direction === 'save'),
      subject: c.previews.find(preview => preview.direction === 'save')?.commitSubject };
  });
  assert(evidence.samePreviews && !evidence.removals.length && evidence.frames > 0 && !evidence.frameFailures.length
    && evidence.rows.every(row => row.connected && row.expanded && row.loaded && row.diffSame && row.semanticSame && !row.wrap
      && row.preSame && row.detailSame && row.detailOpen && row.semanticNodeSame && row.scrollSame && row.top > 0 && row.left > 0
      && row.nestedSame && row.nestedOpen), `${label}: continuity ${JSON.stringify(evidence)}`);
  assert((evidence.requestedDirection !== 'save' && !evidence.hasSave) || evidence.subject === 'Keep my reviewed subject', `${label}: Save subject reset`);
  return evidence;
}
async function ordinary(transport, direction) {
  const preview = page.locator(`ha-ops-preview[direction="${direction}"]`), row = preview.locator('ha-ops-preview-file').first();
  const requests = diffRequests, navigation = navigations;
  for (const action of ['check', 'uncheck', 'all', 'none', 'all', 'ha', 'git']) {
    const sendCount = realEnvelopes.length;
    const before = await authority(direction), rowPath = before.rows[0].path;
    const expected = expectedAfter(before, action, rowPath);
    if (action === 'check' || action === 'uncheck') await row.locator('vaadin-checkbox').click();
    else if (action === 'all' || action === 'none') await preview.getByRole('button', { name: action === 'all' ? 'Select All' : 'Select None', exact: true }).click();
    else await row.getByRole('button', { name: action === 'ha' ? 'Use HA Version' : 'Use Git Version', exact: true }).click();
    assert(realEnvelopes.length === sendCount + 1, `${action}: real decision did not send exactly once`);
    await settle(); await retained(`${transport}/${direction}/${action}`);
    await assertAuthority(direction, expected, `${transport}/${direction}/${action}/ordinary`);
  }
  assert(diffRequests === requests && navigations === navigation, 'decision refetched loaded diff or navigated');
  await page.screenshot({ path: path.join(artifacts, `${transport}-${direction}-retained.png`), fullPage: true });
  results.push({ transport, direction, ordinary: true, extraDiffRequests: diffRequests - requests, navigation: navigations - navigation });
}
async function installDelay(transport) {
  await page.evaluate(transport => {
    const a = document.querySelector('ha-ops-app');
    if (window.held) throw new Error('delayed interception installed twice');
    a.shouldReconnect = false;
    // Production's anonymous addEventListener callback resolves this.receive
    // when it fires. Fence that entry, not the unrelated onmessage attribute.
    // Keep the bound production receiver exclusively for simulated result acks.
    const receiveSimulated = a.receive.bind(a);
    window.transportIsolation = { transport, blockedFrames: [], blockedBaselines: 0, blockedPolls: [], blockedFailures: 0, blockedConnections: [], blockedSocketLifecycle: [] };
    // Late HTTP catch paths bypass baseline delivery. Only explicitly tagged
    // synthetic lifecycle failures may call the real production handlers.
    const markUnknownSimulated = a.markUnknown.bind(a), setConnectionSimulated = a.setConnection.bind(a);
    const syntheticErrors = new WeakSet(); let syntheticLifecycle = false;
    const simulateLifecycle = callback => {
      const previous = syntheticLifecycle; syntheticLifecycle = true;
      try { return callback(); } finally { syntheticLifecycle = previous; }
    };
    a.markUnknown = error => {
      if (syntheticLifecycle || syntheticErrors.has(error)) return simulateLifecycle(() => markUnknownSimulated(error));
      window.transportIsolation.blockedFailures++;
    };
    a.setConnection = connection => {
      if (syntheticLifecycle) return setConnectionSimulated(connection);
      window.transportIsolation.blockedConnections.push(connection);
    };
    window.simulateDisconnect = message => simulateLifecycle(() => markUnknownSimulated(new Error(message)));
    if (a.socket) for (const type of ['open', 'close', 'error']) a.socket.addEventListener(type, event => {
      event.stopImmediatePropagation(); window.transportIsolation.blockedSocketLifecycle.push(type);
    }, { capture: true });
    a.receive = frame => { window.transportIsolation.blockedFrames.push(frame.type); };
    // A GET already awaiting JSON can still resume after timers are cleared.
    // All HTTP state paths converge here; false also stops loadHttpBaseline.
    a.applyBaseline = () => { window.transportIsolation.blockedBaselines++; return false; };
    clearTimeout(a.httpPollTimer); a.httpPollTimer = null;
    clearTimeout(a.reconnectTimer); a.reconnectTimer = null;
    for (const method of ['connect', 'scheduleHttpPoll', 'loadHttpBaseline', 'pollCommandState', 'pollHttpCommand'])
      a[method] = async () => { window.transportIsolation.blockedPolls.push(method); };
    window.held = [];
    window.deepFocus = () => { let n = document.activeElement; while (n?.shadowRoot?.activeElement) n = n.shadowRoot.activeElement; return n; };
    const dispatch = a.dispatchCommand.bind(a);
    a.dispatchCommand = (...args) => {
      const first = !a.commandIntent && /^(select|resolve)_(apply|save)_preview$/.test(args[0]);
      const result = dispatch(...args);
      if (first) {
        // The production method must install its fence in this same JS turn.
        for (const command of ['select_apply_preview', 'resolve_apply_preview', 'select_save_preview', 'resolve_save_preview', 'apply', 'save'])
          dispatch(command, 'unused-fenced-action', {});
        for (const p of a.querySelectorAll('ha-ops-preview')) { p.selectAll(true); p.selectAll(false); p.runFinalAction(); }
      }
      return result;
    };
    if (transport === 'ws') {
      a.socket.send = raw => {
        const envelope = JSON.parse(raw);
        // A queued open listener can run before our lifecycle observer and
        // attempt replay. Keep real replay traffic outside captured decisions.
        if (envelope.command === 'replay') { window.transportIsolation.blockedSocketLifecycle.push('replay-send'); return; }
        window.held.push({ envelope });
      };
    } else {
      const original = window.fetch;
      window.fetch = (url, options) => options?.method === 'POST' ? new Promise((resolve, reject) => window.held.push({ envelope: JSON.parse(options.body), resolve, reject })) : original(url, options);
    }
    window.recordDecision = status => {
      const e = window.held.at(-1).envelope;
      a.state = { ...a.state, command_records: { ...a.state.command_records, [e.command_id]: { command: e.command, status } } };
      a.reconcileAcceptedCommand(); a.requestUpdate();
    };
    window.completeDecision = (outcome, terminal = false) => {
      const held = window.held.at(-1), e = held.envelope;
      if (outcome) {
        if (transport === 'ws') receiveSimulated({ type: 'result', id: e.id, ok: outcome === 'success', message: 'explicit refusal' });
        else if (outcome === 'ambiguous') { const error = new Error('transport failure'); syntheticErrors.add(error); held.reject(error); }
        else held.resolve({ ok: outcome === 'success', json: async () => ({ ok: outcome === 'success', message: 'explicit refusal' }) });
      }
      if (terminal) {
        const d = e.command.includes('save') ? 'save' : 'apply', payload = e.payload;
        const selected = new Set(a.state[`${d}_preview_selected_paths`] || []);
        if (payload.selection_action) { selected.clear(); if (payload.selection_action === 'all') for (const path of a.state[d === 'save' ? 'last_save_preview_paths' : 'last_preview_paths']) selected.add(path); }
        else if ('selected' in payload) { if (payload.selected === '1') selected.add(payload.path); else selected.delete(payload.path); }
        a.state = { ...a.state, [`${d}_preview_selected_paths`]: [...selected],
          [`${d}_preview_resolutions`]: payload.choice ? { ...a.state[`${d}_preview_resolutions`], [payload.path]: payload.choice } : a.state[`${d}_preview_resolutions`],
          [`${d}_decision_revision`]: Number(a.state[`${d}_decision_revision`] || 0) + 1,
          command_records: { ...a.state.command_records, [e.command_id]: { command: e.command, status: 'terminal' } } };
        a.reconcileAcceptedCommand(); a.requestUpdate();
      }
    };
  }, transport);
}
// Deliver late data through the actual production callback boundary while
// seeded authority and (when pending) its intent/focus/content must survive.
async function proveTransportIsolation(transport, direction, label) {
  const proof = await page.evaluate(async ({ transport, direction }) => {
    const a = document.querySelector('ha-ops-app'), boundary = window.transportIsolation;
    const original = { state: a.state, view: a.view, revision: a.revision, intent: a.commandIntent,
      focus: a.decisionFocus, display: a.decisionDisplay, connection: a.connection, replay: a.replayPending,
      accepted: a.acceptedCommandId, uncertain: a.uncertainCommandId, pending: [...a.pending.keys()] };
    const counts = { frames: boundary.blockedFrames.length, baselines: boundary.blockedBaselines };
    for (const revision of [original.revision, original.revision + 1]) {
      const frame = { type: 'state', schema_version: 1, revision,
        state: { ...original.state, [`${direction}_preview_id`]: 'late-real-overwrite',
          [`${direction}_preview_selected_paths`]: ['late-real-path'],
          [`${direction}_preview_resolutions`]: { 'late-real-path': 'ha' },
          operation_generation: Number(original.state.operation_generation) + 1 }, view: {} };
      if (transport === 'ws') a.socket.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(frame) }));
      else a.receive(frame);
      if (a.applyBaseline(frame) !== false) throw new Error('late HTTP baseline was accepted');
    }
    // Timer and command-poll entry points must be paused, too. Previously
    // started successful responses still hit the fenced applyBaseline above.
    for (const method of ['scheduleHttpPoll', 'loadHttpBaseline', 'pollCommandState', 'pollHttpCommand']) await a[method]('late-real-command');
    if (a.state !== original.state || a.view !== original.view || a.revision !== original.revision
      || a.commandIntent !== original.intent || a.decisionFocus !== original.focus || a.decisionDisplay !== original.display
      || a.connection !== original.connection || a.replayPending !== original.replay
      || a.acceptedCommandId !== original.accepted || a.uncertainCommandId !== original.uncertain
      || JSON.stringify([...a.pending.keys()]) !== JSON.stringify(original.pending)) throw new Error('late real transport crossed delayed authority boundary');
    if (boundary.blockedFrames.length - counts.frames !== 2 || boundary.blockedBaselines - counts.baselines !== 2)
      throw new Error('actual transport boundary did not intercept the late frames');
    return { actualSocketMessageEvents: transport === 'ws' ? 2 : 0, sameAndNewRevisionBlocked: true,
      lateBaselinesBlocked: 2, futurePollEntriesPaused: true, stateAndIntentIdentical: true,
      seededIdentity: a.state[`${direction}_preview_id`], pendingCommand: a.commandIntent?.id || null };
  }, { transport, direction });
  await retained(`${transport}/${direction}/${label}/late-real-transport`);
  results.push({ transport, direction, isolation: label, proof });
}
async function delayed(transport, direction) {
  const preview = page.locator(`ha-ops-preview[direction="${direction}"]`), row = preview.locator('ha-ops-preview-file').first();
  const requests = diffRequests, navigation = navigations;
  await proveTransportIsolation(transport, direction, 'loaded-before-action');
  let preAction;
  const phase = async label => {
    const continuity = await retained(`${transport}/${direction}/${label}`);
    const evidence = await page.evaluate(() => {
      const a = document.querySelector('ha-ops-app'), previews = [...a.querySelectorAll('ha-ops-preview')];
      return { sends: window.held.length, blocked: a.mutationBlocked(),
        sameFocus: window.deepFocus() === window.originalFocusTarget,
        focusIntent: !!a.decisionFocus, focusRestorations: window.focusCalls.filter(call => call.preventScroll),
        controlsDisabled: previews.every(p => p.running && p.isFinalActionDisabled()
          && [...p.renderRoot.querySelectorAll('ha-ops-preview-file')].every(r => r.running)),
        disabledStyles: previews.flatMap(p => [...p.renderRoot.querySelectorAll('vaadin-button[disabled]')]).map(b => {
          const s = getComputedStyle(b); return [s.backgroundColor, s.color, s.borderColor]; }) };
    });
    assert(evidence.blocked && evidence.controlsDisabled && !evidence.focusRestorations.length, `${label}: early authority/focus ${JSON.stringify(evidence)}`);
    assert(evidence.disabledStyles.length > 0 && evidence.disabledStyles.every(style => JSON.stringify(style)
      === JSON.stringify(['rgb(229, 231, 235)', 'rgb(107, 114, 128)', 'rgb(209, 213, 219)'])), 'disabled button styles');
    const authority = await assertAuthority(direction, preAction, label);
    return { ...evidence, authority, frames: continuity.frames };
  };
  async function activate(action, enforceAction = false) {
    preAction = await authority(direction);
    const rowPath = preAction.rows[0].path;
    const effectiveAction = ['check', 'uncheck'].includes(action)
      ? (preAction.selected.includes(rowPath) ? 'uncheck' : 'check') : action;
    assert(!enforceAction || effectiveAction === action, `${action}: invalid pre-action selection`);
    const expected = expectedAfter(preAction, effectiveAction, rowPath);
    const control = action === 'check' || action === 'uncheck' ? row.locator('vaadin-checkbox')
      : action === 'all' || action === 'none' ? preview.getByRole('button', { name: action === 'all' ? 'Select All' : 'Select None', exact: true })
      : row.getByRole('button', { name: action === 'ha' ? 'Use HA Version' : 'Use Git Version', exact: true });
    const count = await page.evaluate(() => window.held.length);
    await control.evaluate(control => {
      window.restoreFocusMethod?.();
      control.focus(); window.originalControl = control; window.originalFocusTarget = window.deepFocus();
      window.focusCalls = []; window.nativeFocusLost = false;
      const onBlur = () => { window.nativeFocusLost = true; };
      window.originalFocusTarget.addEventListener('blur', onBlur);
      const target = window.originalFocusTarget, original = target.focus;
      target.focus = function(options) { window.focusCalls.push({ preventScroll: options?.preventScroll === true }); return original.call(this, options); };
      window.restoreFocusMethod = () => { target.focus = original; target.removeEventListener('blur', onBlur); };
    });
    const focused = await control.evaluate(c => window.originalFocusTarget === (c.focusElement || c));
    assert(focused, `${action}: did not capture actual Vaadin native/host focus target`);
    // Actual keyboard events reach the shipped Vaadin component; its button
    // host and checkbox native input have different mandatory blur behavior.
    await control.press(action === 'check' || action === 'uncheck' ? 'Space' : 'Enter');
    await page.waitForFunction(count => window.held.length === count + 1 && window.originalControl.disabled, count);
    const pending = await phase(`${action}/pending`);
    assert(pending.sends === count + 1, 'synchronous decisions or Confirm emitted a second UUID');
    if (action === 'check' || action === 'uncheck') assert(!pending.sameFocus, 'disabled checkbox did not undergo native blur');
    const envelope = await page.evaluate(() => window.held.at(-1).envelope);
    const identity = envelope.payload.preview_identity;
    if (['check', 'uncheck'].includes(effectiveAction)) assert(envelope.payload.selected === (effectiveAction === 'check' ? '1' : ''), 'wrong actual selected wire value');
    else if (['all', 'none'].includes(action)) assert(envelope.payload.selection_action === action, 'wrong selection action');
    else assert(envelope.payload.choice === action && envelope.payload.path === rowPath, 'wrong HA/Git choice');
    assert(envelope.command === `${['ha', 'git'].includes(action) ? 'resolve' : 'select'}_${direction}_preview`, 'wrong direction/command');
    assert(identity.direction === direction && Number.isInteger(identity.decision_revision) && identity.preview_id
      && identity.commit && identity.fingerprint && identity.diff_cursor?.artifact && identity.diff_cursor.generation === Number(await page.locator('ha-ops-app').evaluate(a => a.state.operation_generation)) && sameSet(identity.paths, preAction.paths), 'authority identity was weakened');
    return { control, count: count + 1, pending, expected, envelope };
  }
  for (const [index, action] of ['check', 'uncheck', 'all', 'none', 'all', 'ha', 'git'].entries()) {
    const { count, pending, expected, envelope } = await activate(action, true);
    if (index === 0) await proveTransportIsolation(transport, direction, 'pending-intent');
    if (index === 0 || action === 'ha') await page.screenshot({ path: path.join(artifacts, `${transport}-${direction}-${action}-pending.png`), fullPage: true });
    await page.evaluate(() => window.completeDecision('success'));
    await page.waitForFunction(() => document.querySelector('ha-ops-app').acceptedCommandId);
    const phases = { pending, gap: await phase(`${action}/acceptance-gap`) };
    for (const status of ['accepted', 'running', 'failed_unknown']) {
      await page.evaluate(status => window.recordDecision(status), status);
      phases[status] = await phase(`${action}/${status}`);
    }
    await page.evaluate(() => window.completeDecision(null, true)); await settle();
    await page.waitForFunction(() => !document.querySelector('ha-ops-app').decisionFocus);
    const focus = await page.evaluate(() => ({ same: window.deepFocus() === window.originalFocusTarget,
      connected: window.originalFocusTarget.isConnected, disabled: window.originalControl.disabled,
      nativeFocusLost: window.nativeFocusLost, restorations: window.focusCalls.filter(call => call.preventScroll).length, sends: window.held.length }));
    const continuouslyFocused = Object.values(phases).every(p => p.sameFocus) && !focus.nativeFocusLost;
    // Native checkbox disabling blurs it. A Vaadin button can retain focus;
    // a redundant focus() call is unnecessary when that exact target stayed active.
    assert(focus.same && focus.connected && !focus.disabled
      && (continuouslyFocused ? focus.restorations <= 1 : focus.restorations === 1) && focus.sends === count,
      `${action}: same eligible focus not restored exactly at terminal ${JSON.stringify(focus)}`);
    const terminalAuthority = await assertAuthority(direction, expected, `${action}/terminal`);
    const continuity = await retained(`${transport}/${direction}/${action}/terminal`);
    if (index === 0 || action === 'ha') await page.screenshot({ path: path.join(artifacts, `${transport}-${direction}-${action}-terminal.png`), fullPage: true });
    results.push({ transport, direction, action, envelope, expected, terminalAuthority, phases, terminalFocus: { ...focus, continuouslyFocused }, continuity });
  }
  // Deliberate native keyboard/pointer movement cancels the exact target intent.
  for (const cancel of ['tab', 'pointer']) {
    const { count, expected } = await activate('check');
    if (cancel === 'tab') await page.keyboard.press('Tab');
    else await preview.locator('h3').click();
    const chosen = await page.evaluate(() => { window.deliberateTarget = window.deepFocus(); return !document.querySelector('ha-ops-app').decisionFocus; });
    assert(chosen, `${cancel}: native movement did not cancel focus intent`);
    await page.evaluate(() => { window.completeDecision('success'); window.completeDecision(null, true); }); await settle(); await twoFrames();
    assert(await page.evaluate(count => window.deepFocus() === window.deliberateTarget
      && !window.focusCalls.some(call => call.preventScroll) && window.held.length === count, count), `${cancel}: deliberate focus stolen`);
    await assertAuthority(direction, expected, `${cancel}/terminal`);
    await retained(`${transport}/${direction}/${cancel}-cancelled`);
  }
  // DR001: disconnect before the first record keeps only display evidence.
  const { count } = await activate('check');
  await page.evaluate(() => window.simulateDisconnect('connection lost before first record'));
  await retained(`${transport}/${direction}/disconnect-before-record`);
  const disconnected = await page.evaluate(() => {
    const a = document.querySelector('ha-ops-app');
    return { blocked: a.mutationBlocked(), focus: !!a.decisionFocus, display: a.decisionDisplay,
      extraPayload: a.decisionDisplay && ('payload' in a.decisionDisplay || 'target' in a.decisionDisplay) };
  });
  assert(disconnected.blocked && !disconnected.focus && disconnected.display && !disconnected.extraPayload, 'disconnect retained mutation/focus authority');
  // DR003: an exact definitive refusal may settle this detached local ID.
  await page.evaluate(() => { window.completeDecision('rejected'); const a = document.querySelector('ha-ops-app'); a.connection = 'http'; a.replayPending = false; a.requestUpdate(); });
  await settle(); await twoFrames();
  assert(await page.evaluate(count => {
    const a = document.querySelector('ha-ops-app'); return !a.acceptedCommandId && !a.uncertainCommandId && !a.decisionFocus && !a.decisionDisplay
      && !window.focusCalls.some(call => call.preventScroll) && window.held.length === count;
  }, count), 'detached refusal restored focus, retained its local fence or retried');
  await assertAuthority(direction, preAction, 'detached-refusal');
  await retained(`${transport}/${direction}/detached-refusal`);
  assert(diffRequests === requests && navigations === navigation, 'delayed decision refetched loaded diff or navigated');
  results.push({ transport, direction, deliberateTabAndPointerCancellation: true, disconnectBeforeRecord: true,
    detachedRefusal: true, extraDiffRequests: diffRequests - requests, navigation: navigations - navigation });
}
async function invalidation(direction) {
  await page.evaluate(() => { window.continuity.observing = false; window.continuity.observer.disconnect(); window.restoreFocusMethod?.(); });
  const row = page.locator(`ha-ops-preview[direction="${direction}"] ha-ops-preview-file`).first();
  const reset = await row.evaluate(async row => {
    row.cursor = { ...row.cursor, artifact: 'replacement' }; await row.updateComplete;
    return !row.expanded && row.diff === '' && row.semantic === null && row.diffState === 'idle';
  });
  assert(reset, 'real cursor replacement retained old content');
  await page.reload(); await settle();
  assert(await page.locator('ha-ops-preview-file').evaluateAll(rows => rows.every(row => !row.expanded && row.diffState === 'idle')), 'reload restored expansion');
  assert(await page.locator('ha-ops-app').evaluate(a => !a.commandIntent && !a.decisionFocus && !a.decisionDisplay), 'reload restored transient intent');
  results.push({ direction, cursorReset: true, reloadCollapsed: true, reloadIntentCleared: true });
}
// Historical regression uses only real preview content and the real checkbox.
// Modern synthetic review/identity fixtures are intentionally unnecessary here.
async function checkboxBaseline() {
  await page.goto(baseUrl); await settle();
  await page.getByRole('button', { name: 'Preview Git to HA', exact: true }).click(); await settle();
  await expandLoaded('apply');
  const preview = page.locator('ha-ops-preview[direction="apply"]'), row = preview.locator('ha-ops-preview-file').first();
  const before = await row.evaluate(row => {
    const preview = row.getRootNode().host, pre = row.renderRoot.querySelector('pre');
    const detail = row.renderRoot.querySelector('vaadin-details');
    const tracked = [preview, row, pre, detail], roots = new Set([preview.parentNode]);
    function shadows(n) { if (n.shadowRoot) { roots.add(n.shadowRoot); n.shadowRoot.querySelectorAll('*').forEach(shadows); } }
    tracked.forEach(shadows);
    for (const root of roots) if (root.host?.localName === 'vaadin-details') tracked.push(...root.querySelectorAll('[part="content"], [part="summary"], slot'));
    const c = window.baselineProof = { preview, row, pre, detail, tracked, diff: row.diff, removals: [], frames: 0, frameFailures: [], observing: true };
    c.observer = new MutationObserver(records => {
      for (const r of records) for (const n of r.removedNodes) for (const target of tracked)
        if (n === target || n.contains?.(target)) c.removals.push(target.localName);
    });
    for (const root of roots) c.observer.observe(root, { childList: true, subtree: true });
    function sample() {
      if (!c.observing) return;
      c.frames++;
      if (tracked.some(n => !n.isConnected) || !row.expanded || row.diffState !== 'loaded'
        || row.renderRoot.querySelector('pre') !== pre || row.diff !== c.diff || !detail.opened) c.frameFailures.push(c.frames);
      requestAnimationFrame(sample);
    }
    requestAnimationFrame(sample);
    return { path: row.path, expanded: row.expanded, loaded: row.diffState === 'loaded', nonemptyDiff: row.diff.length > 0, observedRoots: roots.size };
  });
  await page.screenshot({ path: path.join(artifacts, 'before-check.png'), fullPage: true });
  await row.locator('vaadin-checkbox').click(); await settle(); await twoFrames();
  const after = await page.evaluate(() => {
    const c = window.baselineProof, p = document.querySelector('ha-ops-preview[direction="apply"]');
    const r = p?.renderRoot.querySelector('ha-ops-preview-file');
    return { samePreview: p === c.preview, sameRow: r === c.row, originalRowConnected: c.row.isConnected,
      expanded: r?.expanded, loaded: r?.diffState === 'loaded', samePre: r?.renderRoot.querySelector('pre') === c.pre,
      sameDiff: r?.diff === c.diff, selected: r?.selected, checked: r?.renderRoot.querySelector('vaadin-checkbox')?.checked, nativeChecked: r?.renderRoot.querySelector('vaadin-checkbox')?.inputElement.checked,
      removals: c.removals, frames: c.frames, frameFailures: c.frameFailures };
  });
  await page.screenshot({ path: path.join(artifacts, 'after-check.png'), fullPage: true });
  const evidence = { probeMode, baselineRevision: process.env.HA_OPS_BROWSER_BASELINE_REVISION || null, bundleOverride: bundleOverride || null, before, after };
  writeFileSync(path.join(artifacts, 'checkbox-baseline.json'), JSON.stringify(evidence, null, 2));
  results.push(evidence);
  assert(after.samePreview && after.sameRow && after.originalRowConnected && after.expanded && after.loaded && after.samePre
    && after.sameDiff && !after.removals.length && after.frames > 0 && !after.frameFailures.length,
    `original checkbox collapsed or detached mounted diff: ${JSON.stringify(after)}`);
  assert(after.selected && after.checked, 'checkbox baseline did not finish the actual selection');
}
try {
  if (probeMode === 'checkbox-baseline') await checkboxBaseline();
  else for (const transport of ['ws', 'http']) {
    for (const direction of ['apply', 'save']) {
      await prepare(transport, direction, 'ordinary'); await ordinary(transport, direction);
    }
    for (const direction of ['apply', 'save']) {
      await prepare(transport, direction, 'delayed'); await delayed(transport, direction);
      await invalidation(direction);
    }
  }
  assert(!errors.length, `page errors: ${errors.join('; ')}`);
  writeFileSync(path.join(artifacts, 'evidence.json'), JSON.stringify({ rendered: true, probeMode, bundleOverride: bundleOverride || null, results, errors }, null, 2));
  console.log(JSON.stringify({ ok: true, rendered: true, artifacts, results }));
} catch (error) {
  await page.screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true }).catch(() => {});
  writeFileSync(path.join(artifacts, 'failure.json'), JSON.stringify({ message: error.message, results, errors, realEnvelopes }, null, 2));
  throw error;
}
// Retain browser and disposable harness for inspection; never close user pages.
process.exit(0);
