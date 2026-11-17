import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, readFileSync, mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const here = path.dirname(fileURLToPath(import.meta.url));
const sharedRoot = "/Users/purportex/Applications/Playwright";
const { chromium } = await import(pathToFileURL(path.join(sharedRoot, "src/runtime.mjs")));
const artifacts = process.env.HA_OPS_BROWSER_ARTIFACTS_DIR || "/private/tmp/ha-ops-backup-browser";
mkdirSync(artifacts, { recursive: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
const ownedPages = [];
const ownedTabIds = [];
const cleanupErrors = [];
const ownedSessions = [];
const pendingObservers = new Set();
let cleanupStarted = false;
let child = null;
let context = null;
let baseUrl = null;
let harnessRoot = null;
const runtime = { runnerPid: process.pid, ownedTabIds, ownedPages: [], acquisitions: [] };
const writeRuntime = () => writeFileSync(`${artifacts}/runtime.json`, JSON.stringify(runtime, null, 2));
const recordCleanupError = (stage, error) => {
  cleanupErrors.push({ stage, message: String(error?.message || error) });
  process.exitCode = 1;
};
const bounded = async (operation, label, milliseconds = 5000) => {
  let timeout;
  try {
    return await Promise.race([operation, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds);
    })]);
  } finally { clearTimeout(timeout); }
};
const fixtureJson = async (url, options = {}) => {
  const response = await bounded(fetch(url, { ...options, signal: AbortSignal.timeout(5000) }), "fixture response");
  return bounded(response.json(), "fixture response body");
};
const childExited = () => !child || child.exitCode !== null || child.signalCode !== null;
const stopHarness = async () => {
  if (childExited()) return;
  // Observe exit before signaling so fast exits cannot race the listener.
  const exited = new Promise(resolve => child.once("exit", resolve));
  for (const signal of ["SIGINT", "SIGTERM"]) {
    if (childExited()) return;
    try {
      child.kill(signal);
      await bounded(exited, `harness ${signal} exit`);
      return;
    } catch (error) { recordCleanupError(`harness ${signal}`, error); }
  }
};
const closeOwnedPage = async (tab, label) => {
  try { if (!tab.isClosed()) await bounded(tab.close(), label); }
  catch (error) { recordCleanupError(label, error); }
};
const detachOwnedSession = async ownership => {
  if (ownership.detached) return;
  if (ownership.detaching) return ownership.detaching;
  ownership.detaching = (async () => {
    try { await bounded(ownership.session.detach(), "CDP detach"); ownership.detached = true; }
    catch (error) { recordCleanupError("CDP detach", error); }
  })();
  return ownership.detaching;
};
const acquireBounded = async (operation, label, onArrival, mayAcquireResource = true) => {
  const entry = { label, state: "pending", timedOut: false, mayAcquireResource };
  runtime.acquisitions.push(entry);
  let expired = false;
  // Attach the ownership handler before racing: a late returned handle still
  // belongs only to this request and is cleaned during the limited grace period.
  const observed = Promise.resolve(operation).then(async value => {
    entry.state = "handling";
    try { await onArrival?.(value, expired || cleanupStarted); entry.state = "resolved"; return value; }
    catch (error) { entry.state = "rejected"; throw error; }
  }, error => { entry.state = "rejected"; throw error; });
  pendingObservers.add(observed);
  observed.then(() => pendingObservers.delete(observed), () => pendingObservers.delete(observed));
  try { return await bounded(observed, label); }
  catch (error) { expired = true; entry.timedOut = entry.state === "pending" || entry.state === "handling"; throw error; }
};
const acquirePage = async () => {
  const tab = await acquireBounded(context.newPage(), "owned page acquisition", async (page, late) => {
    ownedPages.push(page);
    runtime.ownedPages.push({ index: ownedPages.length - 1, targetId: null });
    if (late) {
      try { writeRuntime(); } catch (error) { recordCleanupError("late page ownership evidence", error); }
      await closeOwnedPage(page, "late owned page close");
    } else writeRuntime();
  });
  let ownership;
  const session = await acquireBounded(context.newCDPSession(tab), "owned CDP session acquisition", async (value, late) => {
    ownership = { session: value, detached: false, detaching: null };
    ownedSessions.push(ownership);
    if (late) await detachOwnedSession(ownership);
  });
  try {
    const { targetInfo } = await acquireBounded(session.send("Target.getTargetInfo"), "owned tab identity", null, false);
    const pageOwnership = runtime.ownedPages[ownedPages.indexOf(tab)];
    pageOwnership.targetId = targetInfo.targetId;
    ownedTabIds.push(targetInfo.targetId);
    writeRuntime();
  } finally { await detachOwnedSession(ownership); }
  return tab;
};
try {
  // An explicit task-owned root permits cleanup even before readiness is printed.
  harnessRoot = mkdtempSync(path.join(os.tmpdir(), "ha-ops-backup-browser-"));
  runtime.harnessRoot = harnessRoot;
  child = spawn("python3", [path.resolve(here, "../../dev_harness.py"), "--root", harnessRoot, "--port", "0", "--print-json"], { stdio: ["ignore", "pipe", "inherit"] });
  child.on("error", error => recordCleanupError("harness process", error));
  runtime.harnessPid = child.pid;
  writeRuntime();
  const ready = await bounded(new Promise((resolve, reject) => {
    let out = "";
    const failed = error => { dispose(); reject(error); };
    const exited = code => failed(new Error(`harness exited ${code}`));
    const data = chunk => {
      out += chunk;
      while (out.includes("\n")) {
        const end = out.indexOf("\n");
        const line = out.slice(0, end);
        out = out.slice(end + 1);
        if (!line.startsWith("{")) continue;
        try { const payload = JSON.parse(line); dispose(); resolve(payload); }
        catch (error) { failed(error); }
        return;
      }
    };
    const dispose = () => {
      child.stdout.removeListener("data", data);
      child.removeListener("error", failed);
      child.removeListener("exit", exited);
    };
    child.stdout.on("data", data);
    child.once("error", failed);
    child.once("exit", exited);
  }), "harness readiness", 30000);
  baseUrl = ready.baseUrl;
  runtime.baseUrl = baseUrl;
  const existingBrowser = process.env.HA_OPS_BROWSER_CDP_URL ? await acquireBounded(chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL), "browser connection") : null;
  context = existingBrowser ? existingBrowser.contexts()[0] : await acquireBounded(chromium.launchPersistentContext(path.join(sharedRoot, "user-data/google-chrome"), {
    channel: "chrome", headless: false, viewport: { width: 1600, height: 1000 }, args: ["--profile-directory=Default", "--remote-debugging-port=9227"],
  }), "persistent browser launch");
  assert(context, "shared browser context unavailable");
  runtime.existingPages = context.pages().map(p => p.url());
  const page = await acquirePage();
  const seed = async (policy, hours = 24) => {
    const response = await fixtureJson(`${baseUrl}__dev_harness__/backup-policy`, { method: "POST", body: new URLSearchParams({ policy, hours: String(hours) }) });
    assert(response.ok, "seed policy failed");
  };
  const state = async () => (await fixtureJson(`${baseUrl}api/v1/state`)).state;
  const settle = async () => page.waitForFunction(() => {
    const app = document.querySelector("ha-ops-app");
    return app && !app.mutationBlocked();
  });
  const warningText = component => component.getByRole('alert').evaluate(alert =>
    `${alert.querySelector('ha-ops-status-badge')?.shadowRoot.querySelector('vaadin-badge')?.textContent || ''} ${alert.querySelector('.backup-warning-message')?.textContent || ''}`.replace(/\s+/g, ' ').trim());
  const badgeEvidence = async () => page.evaluate(async () => {
    const app=document.querySelector('ha-ops-app');
    const hosts=[...app.querySelectorAll('ha-ops-status-badge'), ...[...app.querySelectorAll('ha-ops-preview')].flatMap(p=>[...p.shadowRoot.querySelectorAll('ha-ops-status-badge')])];
    const properties=['fontFamily','fontSize','fontWeight','lineHeight','paddingTop','paddingRight','paddingBottom','paddingLeft','borderRadius','borderTopWidth','backgroundColor','color','borderTopColor','marginTop','marginLeft','minWidth'];
    const sample=node=>{const style=getComputedStyle(node),rect=node.getBoundingClientRect();return {style:Object.fromEntries(properties.map(p=>[p,style[p]])),rect:{left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom,width:rect.width,height:rect.height}};};
    const result=[];
    for(const host of hosts) {
      await host.updateComplete;
      const native=host.shadowRoot.querySelector('vaadin-badge'); await native.updateComplete;
      const content=native.shadowRoot.querySelector('[part="content"]');
      const color=host.severity==='version'?'var(--ha-muted)':host.severity==='error'?'var(--ha-error)':host.severity==='running'||host.severity==='conflicts'?'var(--ha-info)':'var(--ha-warning)';
      const reference=document.createElement('div'); reference.textContent=native.textContent;
      reference.style.cssText=`display:inline-flex;align-items:center;padding:6px 12px;border-radius:999px;font-size:.8rem;text-transform:uppercase;font-family:var(--ha-font);font-weight:400;line-height:normal;box-sizing:border-box;background:color-mix(in srgb,${color} ${host.severity==='version'?'10':'14'}%,transparent);color:${color};border:1px solid color-mix(in srgb,${color} ${host.severity==='version'?'28':'30'}%,transparent)`;
      app.append(reference); const expected=sample(reference); reference.remove();
      const bounds=sample(native),hostBounds=sample(host),partBounds=content.getBoundingClientRect();
      result.push({site:host.dataset.testid||'backup-warning',severity:host.severity,label:native.textContent,theme:native.getAttribute('theme'),native:bounds,host:hostBounds,expected,content:{width:partBounds.width,height:partBounds.height,scrollWidth:content.scrollWidth,clientWidth:content.clientWidth,display:getComputedStyle(content).display},accessible:{role:host.getAttribute('role'),nativeRole:native.getAttribute('role'),tabindex:host.getAttribute('tabindex'),nativeTabindex:native.getAttribute('tabindex'),ariaLabel:native.getAttribute('aria-label')},injected:native.querySelector('b')!==null});
    }
    return {registered:Boolean(customElements.get('ha-ops-status-badge')&&customElements.get('vaadin-badge')),width:document.documentElement.clientWidth,badges:result};
  });
  const assertBadges = evidence => {
    assert(evidence.registered && evidence.badges.length >= 3, 'shared/native badges must be defined at every mounted site');
    const warning=evidence.badges.filter(b=>b.theme==='warning');
    assert(warning.length>=2,'header and backup warnings must share severity');
    for(const b of evidence.badges) {
      assert(JSON.stringify(b.native.style)===JSON.stringify(b.expected.style), `title baseline mismatch: ${JSON.stringify(b)}`);
      assert(Math.abs(b.native.rect.height-b.expected.rect.height)<.1 && Math.abs(b.native.rect.width-b.expected.rect.width)<.1, `title baseline geometry mismatch: ${JSON.stringify(b)}`);
      assert(b.label.trim() && b.content.height>0 && b.content.display!=='none' && b.content.scrollWidth<=b.content.clientWidth+1,'native content hidden/clipped');
      assert(b.native.rect.left>=0 && b.native.rect.right<=evidence.width && b.host.rect.left>=0 && b.host.rect.right<=evidence.width,'badge outside viewport');
      assert(!b.accessible.role&&!b.accessible.nativeRole&&!b.accessible.tabindex&&!b.accessible.nativeTabindex&&!b.accessible.ariaLabel,'badge adds interactive/live/duplicate semantics');
    }
    assert(warning.every(b=>JSON.stringify(b.native.style)===JSON.stringify(warning[0].native.style)),'warning style differs across roots');
  };
  const inspectTheme = async name => {
    const evidence=await badgeEvidence();
    writeFileSync(`${artifacts}/${name}-badges.json`,JSON.stringify(evidence,null,2));
    assertBadges(evidence);
    await page.screenshot({path:`${artifacts}/${name}-badges.png`,fullPage:true});
    return evidence;
  };
  const inspectPresentation = async name => {
    const light=await inspectTheme(`${name}-light`);
    await page.locator('ha-ops-app').evaluate(app=>{app.style.cssText='--ha-bg:#101418;--ha-card-bg:#1d232a;--ha-text:#eef1f5;--ha-muted:#bdc5d0;--ha-warning:#ffd46a;--ha-error:#ff7474;--ha-font:Arial,sans-serif;color-scheme:dark';});
    const dark=await inspectTheme(`${name}-dark`);
    assert(light.badges.find(b=>b.severity==='warning').native.style.color!==dark.badges.find(b=>b.severity==='warning').native.style.color,'mounted warning ignores changed HA theme');
    const dynamics=await page.getByTestId('status-badge').evaluate(async host=>{
      const native=host.shadowRoot.querySelector('vaadin-badge'),original={severity:host.severity,label:host.label};
      const records=[];
      for(const severity of ['error','warning','error','running','conflicts','pending decision','interrupted','transport','success','idle']) {
        host.severity=severity;host.label=severity;await host.updateComplete;await native.updateComplete;
        records.push({severity,reflected:host.getAttribute('severity'),theme:native.getAttribute('theme'),text:native.textContent,color:getComputedStyle(native).color});
      }
      host.label='<b> & literal >';await host.updateComplete;await native.updateComplete;
      const literal={text:native.textContent,injected:Boolean(native.querySelector('b'))};
      host.severity=original.severity;host.label=original.label;await host.updateComplete;await native.updateComplete;
      return {records,literal};
    });
    assert(dynamics.records.every(r=>r.reflected===r.severity&&r.text===r.severity&&r.theme===(r.severity==='error'?'error':['warning','pending decision','interrupted','transport'].includes(r.severity)?'warning':['running','conflicts'].includes(r.severity)?'':'success')),'stale severity/label/theme');
    assert(dynamics.records[0].color===dynamics.records[2].color && dynamics.records[0].color!==dynamics.records[1].color,'error/warning palette drift');
    assert(dynamics.literal.text==='<b> & literal >'&&!dynamics.literal.injected,'label injected markup');
    writeFileSync(`${artifacts}/${name}-dynamics.json`,JSON.stringify(dynamics,null,2));
    await bounded(page.setViewportSize({width:390,height:844}),'badge mobile viewport');
    await page.getByTestId('status-badge').evaluate(async host=>{window.badgeMobileOriginal={severity:host.severity,label:host.label};host.severity='pending decision';host.label='pending decision';await host.updateComplete;});
    await inspectTheme(`${name}-dark-mobile`);
    await page.emulateMedia({forcedColors:'active'});
    const forced=await badgeEvidence();
    assert(forced.badges.every(b=>b.content.height>0&&parseFloat(b.native.style.borderTopWidth)>=1),'forced-colors badge hidden');
    await page.screenshot({path:`${artifacts}/${name}-forced-colors.png`,fullPage:true});
    await page.emulateMedia({forcedColors:'none'});
    await page.locator('ha-ops-app').evaluate(app=>app.removeAttribute('style'));
    await inspectTheme(`${name}-light-mobile`);
    await page.getByTestId('status-badge').evaluate(async host=>{Object.assign(host,window.badgeMobileOriginal);delete window.badgeMobileOriginal;await host.updateComplete;});
    await bounded(page.setViewportSize({width:1600,height:1000}),'badge desktop viewport');
  };
  async function preview() {
    await page.getByRole("button", { name: "Preview Git to HA", exact: true }).click();
    const warning = page.getByTestId("preview-backup-warning");
    await warning.waitFor(); await settle();
    assert(await page.locator('ha-ops-preview[direction="apply"]').count() === 0, "early backup refusal built a diff");
    assert((await warning.innerText()).includes("Preview has not been built. You can continue to review changes; Apply will check the backup again."), "preflight support copy missing");
    const refused = await state();
    assert(refused.preview_backup_check.status === "missing" || refused.preview_backup_check.status === "stale", "typed preflight status missing");
    await inspectPresentation(`preflight-${refused.preview_backup_check.status}`);
    await page.reload(); await settle();
    await warning.waitFor();
    assert(await page.getByRole("button", { name: "Continue Preview Without a Fresh Backup", exact: true }).count() === 0, "reload restored preview consent");
    await page.getByRole("button", { name: "Retry Preview Git to HA", exact: true }).click();
    await page.getByRole("button", { name: "Continue Preview Without a Fresh Backup", exact: true }).waitFor(); await settle();
    await page.getByRole("button", { name: "Continue Preview Without a Fresh Backup", exact: true }).click();
    const component = page.locator('ha-ops-preview[direction="apply"]');
    await component.waitFor();
    await settle();
    assert(await component.evaluate(p => p.selectedPaths.length === 0), "continued diff selected paths automatically");
    await component.locator('ha-ops-preview-file').first().locator('vaadin-checkbox').click();
    assert(await component.evaluate(p => p.paths.length === 2 && p.selectedPaths.length === 1), "fixture must retain a partial one-of-two draft");
    await component.getByRole("button", { name: "Apply Git to HA", exact: true }).waitFor();
    await settle();
    return component;
  }
  if (process.env.HA_OPS_BROWSER_BUNDLE_OVERRIDE) {
    await page.route('**/assets/ha-ops.js', route => route.fulfill({ contentType: 'application/javascript', body: readFileSync(process.env.HA_OPS_BROWSER_BUNDLE_OVERRIDE, 'utf8') }));
  }
  if (process.env.HA_OPS_BROWSER_CSS_OVERRIDE) await page.route('**/assets/ha-ops.css', route => route.fulfill({contentType:'text/css',body:readFileSync(process.env.HA_OPS_BROWSER_CSS_OVERRIDE,'utf8')}));
  await page.goto(baseUrl);
  await settle();
  for (const transport of ["ws", "http"]) {
    if (transport === "http") {
      await page.addInitScript(() => { window.WebSocket = undefined; });
      await page.reload();
      await settle();
      assert(await page.locator("ha-ops-app").evaluate(app => app.connection === "http"), "HTTP fallback not active");
    }
    await seed("missing");
    let component = await preview();
    const before = await component.evaluate(p => ({ selected: [...p.selectedPaths], choices: { ...p.resolutions } }));
    await component.getByRole("button", { name: "Expand All", exact: true }).click();
    await page.waitForFunction(() => [...document.querySelector('ha-ops-preview[direction="apply"]').shadowRoot.querySelectorAll('ha-ops-preview-file')].every(r => r.diffState === 'loaded'));
    await component.evaluate(p => { window.backupDraft = { p, selected: [...p.selectedPaths], rows: [...p.shadowRoot.querySelectorAll('ha-ops-preview-file')] }; });
    await component.getByRole("button", { name: "Apply Git to HA", exact: true }).click();
    const retry = component.getByRole("button", { name: "Retry Git to HA", exact: true });
    const ack = component.getByRole("button", { name: "Acknowledge & Proceed", exact: true });
    await retry.waitFor(); await settle();
    assert((await warningText(component)) === "WARNING No fresh system backup found within 24 hour(s)", "warning text mismatch");
    assert(await component.locator(".backup-warning-badge vaadin-badge").textContent() === "WARNING", "backup warning badge missing");
    assert(await component.locator("ha-ops-status-badge.backup-warning-badge vaadin-badge").getAttribute("theme") === "warning", "warning must be a native Vaadin badge");
    assert(await page.evaluate(() => Boolean(customElements.get("vaadin-badge"))), "native badge undefined");
    assert(await page.locator('[data-testid="status-badge"]').getAttribute('data-status-code') === 'warning', "top status must be warning");
    assert(await retry.getAttribute("theme") === "primary", "Retry is not the primary action");
    assert(await ack.getAttribute("theme") === "secondary", "Acknowledge is not the secondary action");
    assert(await component.getByRole("button", { name: "Cancel", exact: true }).count() === 0, "unrequested Cancel");
    assert(await component.getByRole("button", { name: "Apply Git to HA", exact: true }).count() === 0, "Apply not replaced");
    const rejected = await state();
    assert(await retry.isEnabled() && await ack.isEnabled(), "backup continuation actions must be enabled");
    assert(Object.values(rejected.last_preview_live_fingerprints).some(value => value && typeof value === 'object' && value.counts && value.ids), "rendered fixture must exercise structured fingerprints");
    assert(await component.evaluate(p => p === window.backupDraft.p && window.backupDraft.rows.every(r => r.isConnected && r.expanded && r.diffState === 'loaded') && JSON.stringify(p.selectedPaths) === JSON.stringify(window.backupDraft.selected)), "backup refusal lost mounted review");
    assert(JSON.stringify(before.selected) === JSON.stringify(rejected.apply_preview_selected_paths), "submitted selection lost");
    const dom = await component.evaluate(component => {
      const footer = component.shadowRoot.querySelector("footer");
      const badge = footer.querySelector('ha-ops-status-badge').shadowRoot.querySelector('vaadin-badge');
      const badgeStyle = getComputedStyle(badge);
      return { text: footer.innerText, badge: { tag: badge.localName, text: badge.textContent, theme: badge.getAttribute('theme'), background: badgeStyle.backgroundColor, color: badgeStyle.color, border: badgeStyle.borderColor }, tags: [...footer.querySelectorAll("vaadin-button")].map(e => e.localName),
        actions: [...footer.querySelectorAll("vaadin-button")].map(e => ({ text: e.textContent.trim(), theme: e.getAttribute("theme"), right: e.getBoundingClientRect().right })),
        rects: [...footer.querySelectorAll(".backup-warning, vaadin-button")].map(e => ({ text: e.textContent.trim(), top: e.getBoundingClientRect().top, bottom: e.getBoundingClientRect().bottom })) };
    });
    assert(dom.tags.length === 2, "continuations must be Vaadin controls");
    assert(dom.actions[0].text === "Acknowledge & Proceed" && dom.actions[0].theme === "secondary", "Acknowledge must precede Retry as the secondary action");
    assert(dom.actions[1].text === "Retry Git to HA" && dom.actions[1].theme === "primary" && dom.actions[0].right < dom.actions[1].right, "Retry must occupy the final Apply action position");
    assert(Math.max(...dom.rects.map(r => r.top)) < Math.min(...dom.rects.map(r => r.bottom)), "warning and buttons not on same desktop row");
    await page.screenshot({ path: `${artifacts}/${transport}-backup-warning.png`, fullPage: true });
    await inspectPresentation(`${transport}-apply`);
    const commandCount = Object.keys(rejected.command_records).length;
    const reloadCounters = (await fixtureJson(`${baseUrl}__dev_harness__/diagnostics`)).counters;
    await page.locator("ha-ops-app").evaluate(app => { app.setConnection("reconnecting"); app.replayPending = true; });
    await page.waitForFunction(() => [...document.querySelector("ha-ops-preview").shadowRoot.querySelectorAll("footer vaadin-button")].every(button => button.disabled));
    const disabled = await component.evaluate(component => [...component.shadowRoot.querySelectorAll("footer vaadin-button")].map(button => {
      const style = getComputedStyle(button); return { disabled: button.disabled, background: style.backgroundColor, color: style.color, border: style.borderColor };
    }));
    assert(disabled.every(b => b.disabled && b.background === "rgb(229, 231, 235)" && b.color === "rgb(107, 114, 128)" && b.border === "rgb(209, 213, 219)"), `disabled styling mismatch: ${JSON.stringify(disabled)}`);
    await page.screenshot({ path: `${artifacts}/${transport}-backup-disabled.png`, fullPage: true });
    await page.reload(); await settle();
    assert(await ack.count() === 0 && await retry.count() === 0, "reload revived actionable continuation");
    assert(await component.evaluate(p => p.selectedPaths.length === 0 && Object.keys(p.resolutions).length === 0 && !p.submitted && !p.backupRefusal && Boolean(p.backupWarning)), "reload hydrated decisions or hid diagnostic");
    assert((await warningText(component)) === 'WARNING No fresh system backup found within 24 hour(s)', 'reload lost visible warning');
    assert(await page.locator('[data-testid="status-badge"]').getAttribute('data-status-code') === 'warning', 'reload lost warning status');
    assert(await component.getByRole('button', { name: 'Apply Git to HA', exact: true }).isDisabled(), 'empty reload draft enabled Apply');
    await component.evaluate(p => { p.runFinalAction('acknowledge'); p.runFinalAction('retry'); });
    await page.screenshot({ path: `${artifacts}/${transport}-backup-reload.png`, fullPage: true });
    const newTab = await acquirePage();
    writeFileSync(`${artifacts}/runtime.json`, JSON.stringify(runtime, null, 2));
    if (transport === 'http') await newTab.addInitScript(() => { window.WebSocket = undefined; });
    await newTab.goto(baseUrl);
    await newTab.waitForFunction(() => { const app=document.querySelector('ha-ops-app'); return app && !app.mutationBlocked(); });
    const other = newTab.locator('ha-ops-preview[direction="apply"]');
    assert((await warningText(other)) === 'WARNING No fresh system backup found within 24 hour(s)', 'new tab lost visible warning');
    assert(await other.evaluate(p => p.selectedPaths.length === 0 && Object.keys(p.resolutions).length === 0 && !p.submitted && !p.backupRefusal), 'new tab restored continuation');
    assert(await other.getByRole('button', { name: 'Acknowledge & Proceed', exact: true }).count() === 0 && await other.getByRole('button', { name: 'Retry Git to HA', exact: true }).count() === 0, 'new tab revived continuation');
    await other.evaluate(p => { p.runFinalAction('acknowledge'); p.runFinalAction('retry'); });
    assert(Object.keys((await state()).command_records).length === commandCount, 'display or reload submitted a command');
    await newTab.screenshot({ path: `${artifacts}/${transport}-backup-new-tab.png`, fullPage: true });
    await closeOwnedPage(newTab, "new review tab close");
    assert((await state()).apply_backup_refusal.operation_id === rejected.apply_backup_refusal.operation_id, "reload lost refusal evidence");
    const afterReloadCounters = (await fixtureJson(`${baseUrl}__dev_harness__/diagnostics`)).counters;
    assert(afterReloadCounters.backup_gate_calls === reloadCounters.backup_gate_calls, "reload automatically retried Apply");
    assert(afterReloadCounters.backup_acknowledgements === reloadCounters.backup_acknowledgements, "reload automatically acknowledged backup refusal");
    assert(await component.evaluate(p => p.selectedPaths.length === 0), "continued diff selected paths automatically");
    await component.getByRole("button", { name: "Select All", exact: true }).click();
    await component.getByRole("button", { name: "Apply Git to HA", exact: true }).click();
    await ack.waitFor(); await settle();
    const freshRefusal = (await state()).apply_backup_refusal.operation_id;
    const editCounters = (await fixtureJson(`${baseUrl}__dev_harness__/diagnostics`)).counters;
    const checkbox = component.locator('ha-ops-preview-file').first().locator('vaadin-checkbox');
    await checkbox.click(); await checkbox.click();
    assert(await ack.count() === 0 && await retry.count() === 0, "local edit failed to dismiss continuation");
    assert(await component.getByRole('alert').count() === 1, 'local edit hid diagnostic warning');
    assert((await state()).apply_backup_refusal.operation_id === freshRefusal, "local edit sent server mutation");
    assert((await fixtureJson(`${baseUrl}__dev_harness__/diagnostics`)).counters.backup_gate_calls === editCounters.backup_gate_calls, "edit reran backup gate");
    await component.getByRole("button", { name: "Apply Git to HA", exact: true }).click();
    await ack.waitFor(); await settle();
    await bounded(page.setViewportSize({ width: 390, height: 844 }), "mobile viewport update");
    const mobile = await component.evaluate(component => {
      const footer = component.shadowRoot.querySelector("footer");
      const warning = footer.querySelector('.backup-warning');
      return { width: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth, warningRight: warning.getBoundingClientRect().right, warningText: warning.querySelector('ha-ops-status-badge').shadowRoot.querySelector('vaadin-badge').textContent + warning.innerText, right: footer.getBoundingClientRect().right,
        buttons: [...footer.querySelectorAll("vaadin-button")].map(button => ({ width: button.getBoundingClientRect().width, left:button.getBoundingClientRect().left, right:button.getBoundingClientRect().right, top:button.getBoundingClientRect().top, bottom:button.getBoundingClientRect().bottom, text: button.textContent.trim() })) };
    });
    assert(mobile.right <= mobile.width && mobile.warningRight <= mobile.width && mobile.scrollWidth <= mobile.width && mobile.warningText.includes("WARNING") && mobile.buttons.every(button => button.width > 0 && button.left >= 0 && button.right <= mobile.width && button.bottom > button.top), `mobile footer overflows or hides action: ${JSON.stringify(mobile)}`);
    await page.screenshot({ path: `${artifacts}/${transport}-backup-mobile.png`, fullPage: true });
    await bounded(page.setViewportSize({ width: 1600, height: 1000 }), "desktop viewport update");
    await retry.click();
    await page.waitForFunction(oldId => {
      const app = document.querySelector("ha-ops-app");
      return !app.mutationBlocked() && app.state.apply_backup_refusal?.operation_id && app.state.apply_backup_refusal.operation_id !== oldId;
    }, rejected.apply_backup_refusal.operation_id);
    assert((await state()).apply_backup_refusal.operation_id !== rejected.apply_backup_refusal.operation_id, "Retry did not rerun backup gate");
    await seed("fresh");
    await retry.click();
    await page.waitForFunction(() => { const app = document.querySelector("ha-ops-app"); return !app.mutationBlocked() && !app.state.apply_backup_refusal && app.state.last_status === "success" && app.state.last_action === "apply"; });
    assert(!(await state()).apply_backup_refusal, "fresh Retry kept warning");
    assert(await page.locator('.backup-warning-badge').count()===0 && await page.getByTestId('status-badge').getAttribute('severity')==='success','fresh Retry kept a stale badge');
    await seed("missing");
    component = await preview();
    await component.getByRole("button", { name: "Apply Git to HA", exact: true }).click();
    await ack.waitFor(); await settle();
    const diagnosticsBefore = await fixtureJson(`${baseUrl}__dev_harness__/diagnostics`);
    const ackSelected = await component.evaluate(p => [...p.selectedPaths]);
    assert(ackSelected.length === 1, "acknowledgement must retain the partial draft");
    await component.evaluate(p => p.addEventListener('ha-ops-command', event => { window.backupAckPayload = structuredClone(event.detail.payload); }, { once: true }));
    await ack.click();
    await page.waitForFunction(() => { const app = document.querySelector("ha-ops-app"); return !app.mutationBlocked() && !app.state.apply_backup_refusal && app.state.last_status === "success" && app.state.last_action === "apply"; });
    const diagnosticsAfter = await fixtureJson(`${baseUrl}__dev_harness__/diagnostics`);
    const acknowledged = await page.evaluate(() => window.backupAckPayload);
    assert(acknowledged?.backup_mode === 'acknowledge' && JSON.stringify(acknowledged.selected_paths) === JSON.stringify(ackSelected), "acknowledgement broadened selected paths");
    assert(!(await state()).apply_backup_refusal, "acknowledgement kept warning");
    assert(await page.locator('.backup-warning-badge').count()===0 && await page.getByTestId('status-badge').getAttribute('severity')==='success','acknowledgement kept a stale badge');
    assert(diagnosticsBefore.counters.backup_gate_calls === diagnosticsAfter.counters.backup_gate_calls, "ack called backup gate");
    assert(diagnosticsAfter.counters.backup_acknowledgements === diagnosticsBefore.counters.backup_acknowledgements + 1, "ack missing");
    assert(diagnosticsAfter.counters.backup_inventory_queries === diagnosticsBefore.counters.backup_inventory_queries + 1, "ack did not recheck inventory");
    await seed("stale", 12);
    component = await preview();
    await component.getByRole('button', { name: 'Apply Git to HA', exact: true }).click();
    await ack.waitFor(); await settle();
    const staleRefusal = (await state()).apply_backup_refusal;
    assert(staleRefusal.max_age_hours === 12, 'configured limit not preserved');
    await page.reload(); await settle();
    assert((await warningText(component)) === 'WARNING No fresh system backup found within 12 hour(s)', 'stale configured-limit warning missing on reload');
    assert(await ack.count() === 0 && await retry.count() === 0, 'stale reload revived continuation');
    await page.screenshot({ path: `${artifacts}/${transport}-backup-stale-12h.png`, fullPage: true });
    writeFileSync(`${artifacts}/${transport}-evidence.json`, JSON.stringify({ dom, disabled, mobile, reloadCounters, afterReloadCounters, diagnosticsAfter, staleRefusal }, null, 2));
  }
} catch (error) {
  console.error(error.stack); process.exitCode = 1;
}
finally {
  // Each owned resource is independent; a failed tab close cannot block the
  // harness stop, another tab, or evidence. Shared pages/context stay intact.
  cleanupStarted = true;
  await Promise.allSettled([
    ...ownedPages.map((owned, index) => closeOwnedPage(owned, `owned tab ${index} close`)),
    ...ownedSessions.map(detachOwnedSession),
    stopHarness(),
  ]);
  // Never keep the fixture alive while waiting for a browser to recover.
  // A short grace handles delayed resource replies; unresolved requests are
  // explicit uncertainty, not evidence of complete cleanup.
  if (pendingObservers.size) {
    try { await bounded(Promise.allSettled([...pendingObservers]), "late acquisition grace", 1000); }
    catch (error) { recordCleanupError("late acquisition grace", error); }
  }
  const pendingOperations = runtime.acquisitions.filter(entry => ["pending", "handling"].includes(entry.state));
  if (pendingOperations.length) recordCleanupError("uncertain ownership", "Acquisition remains unresolved after grace; late resources may require inspection.");
  let harnessRootRemoved = !harnessRoot;
  if (harnessRoot && childExited()) {
    try { rmSync(harnessRoot, { recursive: true, force: true }); harnessRootRemoved = true; }
    catch (error) { recordCleanupError("fixture removal", error); }
  }
  let remainingPages = null;
  let ownedTabsClosed = false;
  try { ownedTabsClosed = ownedPages.every(p => p.isClosed()); }
  catch (error) { recordCleanupError("owned tab inventory", error); }
  try { remainingPages = context?.pages().map(p => p.url()) || []; }
  catch (error) { recordCleanupError("shared tab inventory", error); }
  try {
    const sessionsDetached = ownedSessions.every(ownership => ownership.detached);
    const cleanupIncomplete = pendingOperations.length > 0 || !ownedTabsClosed || !sessionsDetached || !childExited() || !harnessRootRemoved;
    writeFileSync(`${artifacts}/cleanup.json`, JSON.stringify({ ownedTabIds, ownedPages: runtime.ownedPages,
      acquisitions: runtime.acquisitions, pendingOperations, sessionsDetached, cleanupIncomplete,
      ownedTabsClosed, harnessExited: childExited(), harnessRoot, harnessRootRemoved, remainingPages, errors: cleanupErrors }, null, 2));
  } catch (error) { recordCleanupError("cleanup evidence", error); }
  for (const error of cleanupErrors) console.error(`Cleanup ${error.stage}: ${error.message}`);
}
console.log(JSON.stringify({ ok: !process.exitCode, artifacts, baseUrl, retainedBrowser: true }));
process.exit(process.exitCode || 0);
