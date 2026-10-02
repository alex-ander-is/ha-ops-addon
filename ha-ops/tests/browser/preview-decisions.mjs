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
const harness = await new Promise((resolve, reject) => {
  let output = '';
  child.stdout.on('data', data => { output += data; const line = output.split('\n').find(line => line.startsWith('{')); if (line) resolve(JSON.parse(line)); });
  child.once('exit', code => reject(new Error(`harness exited ${code}`)));
});
const { baseUrl } = harness;
// Preserve the shared context and all existing user pages. No anonymous context.
const existing = process.env.HA_OPS_BROWSER_CDP_URL ? await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL) : null;
const context = existing ? existing.contexts()[0] : await chromium.launchPersistentContext(path.join(sharedRoot, 'user-data/google-chrome'), {
  channel: 'chrome', headless: false, viewport: { width: 1600, height: 1000 },
  args: ['--profile-directory=Default', '--remote-debugging-port=9227'],
});
const pages = context.pages();
const page = pages.find(page => page.url() === 'about:blank') || pages.find(page => page.url().startsWith(baseUrl)) || await context.newPage();
writeFileSync(path.join(artifacts, 'runtime.json'), JSON.stringify({ harnessPid: child.pid, runnerPid: process.pid, harnessRoot: harness.root, baseUrl, existingPages: pages.map(p => p.url()), pageCreated: !pages.includes(page) }, null, 2));
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
  try { const e = JSON.parse(payload); if (/^(?:(?:select|resolve)_(?:apply|save)_preview|apply|save)$/.test(e.command)) realEnvelopes.push(e); } catch {}
}));
page.on('request', request => {
  if (request.method() === 'POST') {
    try { const e = JSON.parse(request.postData()); if (/^(?:(?:select|resolve)_(?:apply|save)_preview|apply|save)$/.test(e.command)) realEnvelopes.push(e); } catch {}
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
    paths: [...p.paths], selected: [...p.selectedPaths],
    choices: { ...p.resolutions },
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
  await fetch(`${baseUrl}__dev_harness__/clear-previews`, { method: 'POST' });
  await page.goto(baseUrl); await settle();
  await page.getByRole('button', { name: direction === 'apply' ? 'Preview Git to HA' : /^(Preview HA to Git|Review Post-Apply HA Changes)$/, exact: true }).click();
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

async function localFlow(transport, direction) {
  const preview = page.locator(`ha-ops-preview[direction="${direction}"]`), row = preview.locator('ha-ops-preview-file').first();
  const sends = realEnvelopes.length, requests = diffRequests;
  await page.locator('ha-ops-app').evaluate(a => {
    window.editDispatches = []; const dispatch = a.dispatchCommand.bind(a);
    a.dispatchCommand = (...args) => { window.editDispatches.push(args[0]); return dispatch(...args); };
  });
  for (const action of ['check', 'uncheck', 'all', 'none', 'all', 'ha', 'git']) {
    const before = await authority(direction), expected = expectedAfter(before, action, before.rows[0].path);
    const control = action === 'check' || action === 'uncheck' ? row.locator('vaadin-checkbox').locator('input[type=checkbox]')
      : action === 'all' || action === 'none' ? preview.getByRole('button', { name: action === 'all' ? 'Select All' : 'Select None', exact: true })
      : row.getByRole('button', { name: action === 'ha' ? 'Use HA Version' : 'Use Git Version', exact: true });
    await control.focus();
    await control.evaluate(c => { let n = document.activeElement; while (n?.shadowRoot?.activeElement) n = n.shadowRoot.activeElement; window.editFocus = n; });
    await control.press(action === 'check' || action === 'uncheck' ? 'Space' : 'Enter');
    await assertAuthority(direction, expected, `${transport}/${direction}/${action}`);
    const immediate = await preview.evaluate(p => {
      const a = document.querySelector('ha-ops-app'); let n = document.activeElement; while (n?.shadowRoot?.activeElement) n = n.shadowRoot.activeElement;
      return { dispatches: window.editDispatches, running: p.running, blocked: a.mutationBlocked(), intent: a.commandIntent,
        focusSame: n === window.editFocus, finalDisabled: p.renderRoot.querySelector('footer vaadin-button').disabled };
    });
    assert(!immediate.dispatches.length && !immediate.running && !immediate.blocked && !immediate.intent && immediate.focusSame
      && immediate.finalDisabled === !expected.selected.length && realEnvelopes.length === sends, `preview edit emitted transport or dispatch: ${JSON.stringify(immediate)}`);
    await retained(`${transport}/${direction}/${action}`);
  }
  assert(diffRequests === requests, 'local edits fetched another diff');
  await page.screenshot({ path: path.join(artifacts, `${transport}-${direction}-local.png`), fullPage: true });
  // A second component mounting from the same server baseline starts empty.
  await page.reload(); await settle(); await waitForRows(direction);
  assert((await authority(direction)).selected.length === 0, 'reload hydrated submitted decisions');
  await expandLoaded(direction);
  await preview.getByRole('button', { name: 'Select All', exact: true }).click();
  const expected = await authority(direction), count = realEnvelopes.length;
  await fetch(`${baseUrl}__dev_harness__/arm`, { method: 'POST', body: new URLSearchParams({ action: direction, gate: 'running' }) });
  await preview.getByRole('button', { name: direction === 'save' ? 'Save HA to Git' : 'Apply Git to HA', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('ha-ops-app').state.last_status === 'running');
  const pending = await preview.evaluate(p => ({ connected: p.isConnected, selected: [...p.selectedPaths], rows: [...p.renderRoot.querySelectorAll('ha-ops-preview-file')].map(r => ({ connected: r.isConnected, loaded: r.diffState === 'loaded', expanded: r.expanded })), disabled: [...p.renderRoot.querySelectorAll('footer vaadin-button')].every(b => b.disabled) }));
  assert(pending.connected && sameSet(pending.selected, expected.selected) && pending.disabled && pending.rows.every(r => r.connected && r.loaded && r.expanded), `final action lost mounted draft: ${JSON.stringify(pending)}`);
  assert(realEnvelopes.length === count + 1, 'final action must send exactly one batch');
  const envelope = realEnvelopes.at(-1);
  assert(envelope.command === direction && sameSet(envelope.payload.selected_paths, expected.selected)
    && JSON.stringify(envelope.payload.resolutions) === JSON.stringify(expected.choices) && envelope.payload.preview_identity.direction === direction, 'incomplete final decision batch');
  await fetch(`${baseUrl}__dev_harness__/release`, { method: 'POST', body: new URLSearchParams({ action: direction, gate: 'running' }) });
  await settle();
  results.push({ transport, direction, localEdits: 7, zeroEditTraffic: true, reloadEmpty: true, pending, envelope });
}
try {
  for (const transport of ['ws', 'http']) for (const direction of ['apply', 'save']) {
    await prepare(transport, direction, 'ordinary'); await localFlow(transport, direction);
  }
  assert(!errors.length, `page errors: ${errors.join('; ')}`);
  writeFileSync(path.join(artifacts, 'evidence.json'), JSON.stringify({ rendered: true, results, errors }, null, 2));
  console.log(JSON.stringify({ ok: true, artifacts, baseUrl, harnessPid: child.pid, harnessRoot: harness.root }));
} catch (error) {
  await page.screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true }).catch(() => {});
  writeFileSync(path.join(artifacts, 'failure.json'), JSON.stringify({ message: error.message, results, errors, realEnvelopes }, null, 2));
  console.error(error.stack); process.exitCode = 1;
}
// Shared profile, existing pages and disposable fixture remain for inspection.
process.exit(process.exitCode || 0);
