import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const here = path.dirname(fileURLToPath(import.meta.url));
const sharedRoot = "/Users/purportex/Applications/Playwright";
const { chromium } = await import(pathToFileURL(path.join(sharedRoot, "src/runtime.mjs")));
const artifacts = process.env.HA_OPS_BROWSER_ARTIFACTS_DIR || "/private/tmp/ha-ops-backup-browser";
mkdirSync(artifacts, { recursive: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
const child = spawn("python3", [path.resolve(here, "../../dev_harness.py"), "--port", "0", "--print-json"], { stdio: ["ignore", "pipe", "inherit"] });
const ready = await new Promise((resolve, reject) => {
  let out = "";
  child.stdout.on("data", chunk => { out += chunk; const line = out.split("\n").find(line => line.startsWith("{")); if (line) resolve(JSON.parse(line)); });
  child.on("exit", code => reject(new Error(`harness exited ${code}`)));
});
const existingBrowser = process.env.HA_OPS_BROWSER_CDP_URL ? await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL) : null;
const context = existingBrowser ? existingBrowser.contexts()[0] : await chromium.launchPersistentContext(path.join(sharedRoot, "user-data/google-chrome"), {
  channel: "chrome", headless: false, viewport: { width: 1600, height: 1000 }, args: ["--profile-directory=Default", "--remote-debugging-port=9227"],
});
const { baseUrl } = ready;
const existingPages = context.pages();
const page = existingPages.find(page => page.url() === "about:blank")
  || existingPages.find(page => page.url().startsWith(baseUrl)) || await context.newPage();
const seed = async policy => {
  const response = await fetch(`${baseUrl}__dev_harness__/backup-policy`, { method: "POST", body: new URLSearchParams({ policy }) });
  assert((await response.json()).ok, "seed policy failed");
};
const state = async () => (await (await fetch(`${baseUrl}api/v1/state`)).json()).state;
const settle = async () => page.waitForFunction(() => {
  const app = document.querySelector("ha-ops-app");
  return app && !app.mutationBlocked();
});
async function preview() {
  await page.getByRole("button", { name: "Preview Git to HA", exact: true }).click();
  const component = page.locator('ha-ops-preview[direction="apply"]');
  await component.waitFor();
  await settle();
  await component.getByRole("button", { name: "Select All", exact: true }).click();
  await component.getByRole("button", { name: "Apply Git to HA", exact: true }).waitFor();
  await settle();
  return component;
}
try {
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
    const before = await state();
    await component.getByRole("button", { name: "Apply Git to HA", exact: true }).click();
    const retry = component.getByRole("button", { name: "Retry Git to HA", exact: true });
    const ack = component.getByRole("button", { name: "Acknowledge & Proceed", exact: true });
    await retry.waitFor(); await settle();
    assert((await component.getByRole("alert").innerText()).replace(/\s+/g, " ").trim() === "ERROR No fresh system backup found within 24 hour(s)", "warning text mismatch");
    assert(await component.locator(".backup-warning-badge").textContent() === "ERROR", "backup warning badge missing");
    assert(await retry.getAttribute("theme") === "primary", "Retry is not the primary action");
    assert(await ack.getAttribute("theme") === "secondary", "Acknowledge is not the secondary action");
    assert(await component.getByRole("button", { name: "Cancel", exact: true }).count() === 0, "unrequested Cancel");
    assert(await component.getByRole("button", { name: "Apply Git to HA", exact: true }).count() === 0, "Apply not replaced");
    const rejected = await state();
    assert(before.apply_preview_id === rejected.apply_preview_id, "preview replaced");
    assert(JSON.stringify(before.apply_preview_selected_paths) === JSON.stringify(rejected.apply_preview_selected_paths), "selection lost");
    const dom = await component.evaluate(component => {
      const footer = component.shadowRoot.querySelector("footer");
      return { text: footer.innerText, tags: [...footer.querySelectorAll("vaadin-button")].map(e => e.localName),
        actions: [...footer.querySelectorAll("vaadin-button")].map(e => ({ text: e.textContent.trim(), theme: e.getAttribute("theme"), right: e.getBoundingClientRect().right })),
        rects: [...footer.querySelectorAll(".backup-warning, vaadin-button")].map(e => ({ text: e.textContent.trim(), top: e.getBoundingClientRect().top, bottom: e.getBoundingClientRect().bottom })) };
    });
    assert(dom.tags.length === 2, "continuations must be Vaadin controls");
    assert(dom.actions[0].text === "Acknowledge & Proceed" && dom.actions[0].theme === "secondary", "Acknowledge must precede Retry as the secondary action");
    assert(dom.actions[1].text === "Retry Git to HA" && dom.actions[1].theme === "primary" && dom.actions[0].right < dom.actions[1].right, "Retry must occupy the final Apply action position");
    assert(Math.max(...dom.rects.map(r => r.top)) < Math.min(...dom.rects.map(r => r.bottom)), "warning and buttons not on same desktop row");
    await page.screenshot({ path: `${artifacts}/${transport}-backup-warning.png`, fullPage: true });
    const reloadCounters = (await (await fetch(`${baseUrl}__dev_harness__/diagnostics`)).json()).counters;
    await page.locator("ha-ops-app").evaluate(app => { app.setConnection("reconnecting"); app.replayPending = true; });
    await page.waitForFunction(() => [...document.querySelector("ha-ops-preview").shadowRoot.querySelectorAll("footer vaadin-button")].every(button => button.disabled));
    const disabled = await component.evaluate(component => [...component.shadowRoot.querySelectorAll("footer vaadin-button")].map(button => {
      const style = getComputedStyle(button); return { disabled: button.disabled, background: style.backgroundColor, color: style.color, border: style.borderColor };
    }));
    assert(disabled.every(b => b.disabled && b.background === "rgb(229, 231, 235)" && b.color === "rgb(107, 114, 128)" && b.border === "rgb(209, 213, 219)"), `disabled styling mismatch: ${JSON.stringify(disabled)}`);
    await page.screenshot({ path: `${artifacts}/${transport}-backup-disabled.png`, fullPage: true });
    await page.reload(); await settle();
    await ack.waitFor();
    assert((await state()).apply_backup_refusal.operation_id === rejected.apply_backup_refusal.operation_id, "reload lost current warning");
    const afterReloadCounters = (await (await fetch(`${baseUrl}__dev_harness__/diagnostics`)).json()).counters;
    assert(afterReloadCounters.backup_gate_calls === reloadCounters.backup_gate_calls, "reload automatically retried Apply");
    assert(afterReloadCounters.backup_acknowledgements === reloadCounters.backup_acknowledgements, "reload automatically acknowledged backup refusal");
    await page.setViewportSize({ width: 390, height: 844 });
    const mobile = await component.evaluate(component => {
      const footer = component.shadowRoot.querySelector("footer");
      return { width: document.documentElement.clientWidth, right: footer.getBoundingClientRect().right,
        buttons: [...footer.querySelectorAll("vaadin-button")].map(button => ({ width: button.getBoundingClientRect().width, text: button.textContent.trim() })) };
    });
    assert(mobile.right <= mobile.width && mobile.buttons.every(button => button.width > 0), `mobile footer overflows or hides action: ${JSON.stringify(mobile)}`);
    await page.screenshot({ path: `${artifacts}/${transport}-backup-mobile.png`, fullPage: true });
    await page.setViewportSize({ width: 1600, height: 1000 });
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
    await seed("missing");
    component = await preview();
    await component.getByRole("button", { name: "Apply Git to HA", exact: true }).click();
    await ack.waitFor(); await settle();
    const diagnosticsBefore = await (await fetch(`${baseUrl}__dev_harness__/diagnostics`)).json();
    await ack.click();
    await page.waitForFunction(() => { const app = document.querySelector("ha-ops-app"); return !app.mutationBlocked() && !app.state.apply_backup_refusal && app.state.last_status === "success" && app.state.last_action === "apply"; });
    const diagnosticsAfter = await (await fetch(`${baseUrl}__dev_harness__/diagnostics`)).json();
    assert(!(await state()).apply_backup_refusal, "acknowledgement kept warning");
    assert(diagnosticsBefore.counters.backup_gate_calls === diagnosticsAfter.counters.backup_gate_calls, "ack called backup gate");
    assert(diagnosticsAfter.counters.backup_acknowledgements === diagnosticsBefore.counters.backup_acknowledgements + 1, "ack missing");
    writeFileSync(`${artifacts}/${transport}-evidence.json`, JSON.stringify({ dom, disabled, mobile, reloadCounters, afterReloadCounters, diagnosticsAfter }, null, 2));
  }
  console.log(JSON.stringify({ ok: true, artifacts, baseUrl, retainedBrowser: true }));
} catch (error) {
  console.error(error.stack);
  console.log(JSON.stringify({ ok: false, artifacts, baseUrl, retainedBrowser: true }));
}
// Keep the shared profile and the local fixture available for inspection.
// The operator owns teardown; this runner never closes existing shared pages.
setInterval(() => {}, 60000);
