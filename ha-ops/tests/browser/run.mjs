import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, "../..");
const repoRoot = path.resolve(appRoot, "..");
const sharedRoot = process.env.PLAYWRIGHT_SHARED_ROOT || "/Users/purportex/Applications/Playwright";
const { chromium } = await import(pathToFileURL(path.join(sharedRoot, "src/runtime.mjs")).href);
const artifactsRoot = process.env.HA_OPS_BROWSER_ARTIFACTS_DIR || path.join(here, "artifacts");
const artifactsDir = path.join(artifactsRoot, new Date().toISOString().replaceAll(":", "").replaceAll(".", ""));
mkdirSync(artifactsDir, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function startHarness() {
  const child = spawn("python3", [path.join(appRoot, "dev_harness.py"), "--port", "0", "--print-json"], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPYCACHEPREFIX: process.env.PYTHONPYCACHEPREFIX || "/private/tmp/ha-ops-browser-pycache" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (part) => { stderr += part.toString(); });
  const ready = new Promise((resolve, reject) => {
    let stdout = "";
    const timeout = setTimeout(() => reject(new Error(`Harness did not start: ${stderr}`)), 10000);
    child.stdout.on("data", (part) => {
      stdout += part.toString();
      const line = stdout.split(/\r?\n/).find((item) => item.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timeout);
      try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
    });
    child.once("exit", (code) => { clearTimeout(timeout); reject(new Error(`Harness exited ${code}: ${stderr}`)); });
  });
  return { child, ready };
}

async function stateAt(baseUrl) {
  const response = await fetch(`${baseUrl}api/v1/state`);
  assert(response.ok, `State API returned ${response.status}`);
  return (await response.json()).state;
}

async function waitForState(baseUrl, predicate, label) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const state = await stateAt(baseUrl);
    if (predicate(state)) return state;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

async function inspectPage(page, label) {
  await page.locator("ha-ops-app").waitFor();
  await page.getByTestId("status-badge").waitFor();
  await page.getByRole("button", { name: "Preview Git to HA" }).waitFor();
  const contour = await page.locator("ha-ops-app").evaluate((app) => {
    const root = app.renderRoot || app.shadowRoot || app;
    return {
      rawControls: root?.querySelectorAll("button, select, textarea, form, input:not([slot='input'])").length,
      rawTags: [...(root?.querySelectorAll("button, select, textarea, form, input:not([slot='input'])") || [])].map((item) => item.outerHTML.slice(0, 100)),
      vaadinButtons: root?.querySelectorAll("vaadin-button").length,
      log: Boolean(root?.querySelector("ha-ops-log")?.shadowRoot?.querySelector("pre")),
      horizontalOverflow: app.scrollWidth > app.clientWidth + 2,
    };
  });
  assert(contour.rawControls === 0, `${label}: legacy HTML control found: ${contour.rawTags.join(" | ")}`);
  assert(contour.vaadinButtons > 0, `${label}: Vaadin buttons missing`);
  assert(contour.log, `${label}: log missing`);
  assert(!contour.horizontalOverflow, `${label}: horizontal overflow`);
  await page.screenshot({ path: path.join(artifactsDir, `${label}.png`), fullPage: true });
}

async function exerciseWorkflow(page, baseUrl, label) {
  await page.reload();
  await page.locator("ha-ops-app").waitFor();
  const initial = await page.locator("ha-ops-app").evaluate(async (app) => {
    await app.updateComplete;
    return { buttons: [...app.querySelectorAll("vaadin-button")].map((item) => item.textContent.trim()),
      connection: app.connection, blocked: app.mutationBlocked(), status: app.state.last_status,
      active: app.state.active_operation };
  });
  const savePreviewLabel = initial.buttons.includes("Preview HA to Git") ? "Preview HA to Git" : "Review Post-Apply HA Changes";
  assert(initial.buttons.includes(savePreviewLabel), `${label} Save preview control absent: ${JSON.stringify(initial)}`);
  await page.getByRole("button", { name: savePreviewLabel }).click();
  const saveState = await waitForState(baseUrl, (state) => state.last_action === "save_preview" && state.last_status === "success", `${label} Save preview`);
  assert(saveState.last_save_preview_paths?.length === 2, `${label} Save preview paths missing`);
  await page.reload();
  const save = page.locator('ha-ops-preview[direction="save"]');
  await save.waitFor();
  await save.getByRole("button", { name: "Select All" }).click();
  await waitForState(baseUrl, (state) => state.save_preview_selected_paths?.length === 2, `${label} Save selection`);
  await save.getByRole("button", { name: "Save HA to Git" }).click();
  await waitForState(baseUrl, (state) => state.last_action === "save" && state.last_status === "success", `${label} Save completion`);
  await page.reload();
  await page.waitForFunction(() => !document.querySelector("ha-ops-app")?.replayPending, undefined, { timeout: 5000 });
  const afterSave = await page.locator("ha-ops-app").evaluate((app) => ({ blocked: app.mutationBlocked(),
    operation: app.state.active_operation, connection: app.connection, replay: app.replayPending,
    status: app.state.last_status, accepted: app.acceptedCommandId }));
  assert(!afterSave.blocked, `${label} Save left controls blocked: ${JSON.stringify(afterSave)}`);
  await page.getByRole("button", { name: "Preview Git to HA" }).click();
  await waitForState(baseUrl, (state) => state.last_action === "preview" && state.last_status === "success", `${label} Apply preview`);
  await page.reload();
  const apply = page.locator('ha-ops-preview[direction="apply"]');
  await apply.waitFor();
  await apply.getByRole("button", { name: "Select All" }).click();
  await waitForState(baseUrl, (state) => state.apply_preview_selected_paths?.length === 2, `${label} Apply selection`);
  await apply.getByRole("button", { name: "Apply Git to HA" }).click();
  await waitForState(baseUrl, (state) => state.last_action === "apply" && state.last_status === "success", `${label} Apply completion`);
  await page.reload();
  await page.waitForFunction(() => !document.querySelector("ha-ops-app")?.replayPending, undefined, { timeout: 5000 });

  // Render the conflict decision states against the same Vaadin components.
  const conflict = await page.locator("ha-ops-app").evaluate(async (app) => {
    const path = "homeassistant/configuration.yaml";
    app.state = { ...app.state, active_operation: null, last_status: "success",
      last_save_preview_paths: [path], last_save_preview_conflict_paths: [path],
      save_preview_selected_paths: [path], save_preview_resolutions: {}, save_preview_id: `${app.state.save_preview_id}-conflict` };
    await app.updateComplete;
    const save = app.querySelector('ha-ops-preview[direction="save"]');
    await save.updateComplete;
    const before = save.isFinalActionDisabled();
    app.state = { ...app.state, save_preview_resolutions: { [path]: "ha" },
      last_preview_paths: [path], last_preview_conflict_paths: [path],
      apply_preview_selected_paths: [path], apply_preview_resolutions: {} };
    await app.updateComplete;
    const apply = app.querySelector('ha-ops-preview[direction="apply"]');
    await apply.updateComplete;
    const file = save.renderRoot.querySelector("ha-ops-preview-file");
    await file.updateComplete;
    return { saveNeedsChoice: before, saveEnabled: !save.isFinalActionDisabled(),
      applyGitDefault: apply.effectiveChoice(path) === "git", applyEnabled: !apply.isFinalActionDisabled(),
      saveChoices: file.renderRoot.querySelectorAll("vaadin-button[aria-pressed]").length };
  });
  assert(conflict.saveNeedsChoice && conflict.saveEnabled && conflict.applyGitDefault && conflict.applyEnabled
    && conflict.saveChoices >= 2, `${label} conflict controls: ${JSON.stringify(conflict)}`);

  const ids = await page.locator("ha-ops-app").evaluate(async (app) => {
    const path = "homeassistant/automations.yaml";
    const row = { path, changes: 1, unresolved: 0, selected: false, diff_sha256: "fixture-digest" };
    app.state = { ...app.state, last_save_preview_paths: [], last_preview_paths: [],
      last_internal_ids_generated_at: "2026-09-29", last_internal_ids_preview_id: "browser-ids-preview",
      last_internal_ids_rows: [row], last_internal_ids_unresolved: [] };
    app.internalDiffs = new Map([[path, "diff --git a/automations.yaml b/automations.yaml"]]);
    await app.updateComplete;
    const section = app.querySelector('[data-testid="internal-ids-preview-section"]');
    const checkbox = section.querySelector("vaadin-checkbox");
    const button = [...section.querySelectorAll("vaadin-button")].find((item) => item.textContent.includes("Migrate"));
    const initiallyDisabled = button?.disabled;
    app.state = { ...app.state, last_internal_ids_rows: [{ ...row, selected: true }] };
    await app.updateComplete;
    const enabled = ![...app.querySelectorAll('[data-testid="internal-ids-preview-section"] vaadin-button')]
      .find((item) => item.textContent.includes("Migrate"))?.disabled;
    return { checkbox: Boolean(checkbox), initiallyDisabled, enabled };
  });
  assert(ids.checkbox && ids.initiallyDisabled && ids.enabled, `${label} Internal IDs controls: ${JSON.stringify(ids)}`);

  const recovery = await page.locator("ha-ops-app").evaluate(async (app) => {
    app.state = { ...app.state, active_operation: { command: "apply", command_id: "fixture",
      phase: "recovery_required", evidence: { guidance: "Review affected targets" } } };
    await app.updateComplete;
    return { alert: Boolean(app.querySelector('[data-testid="operation-recovery"]')),
      previewHidden: !app.querySelector("ha-ops-preview"), blocked: app.mutationBlocked(),
      disabled: [...app.querySelectorAll("vaadin-button")].filter((item) => item.textContent.includes("Preview"))
        .every((item) => item.disabled) };
  });
  assert(recovery.alert && recovery.previewHidden && recovery.blocked && recovery.disabled,
    `${label} recovery controls: ${JSON.stringify(recovery)}`);
  await page.screenshot({ path: path.join(artifactsDir, `${label}-recovery.png`), fullPage: true });
}

const { child, ready } = await startHarness();
let browser;
try {
  const { baseUrl } = await ready;
  browser = await chromium.launch({ headless: true });
  const desktop = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await desktop.newPage();
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => { if (response.status() >= 400) pageErrors.push(`${response.status()} ${response.url()}`); });
  await page.goto(baseUrl);
  await inspectPage(page, "initial-desktop");
  assert((await stateAt(baseUrl)).last_status !== "error", "Initial state is in error");

  const secondTab = await desktop.newPage();
  await secondTab.goto(baseUrl);
  await inspectPage(secondTab, "second-tab-desktop");
  await page.getByRole("button", { name: "Preview Git to HA" }).click();
  const previewState = await waitForState(baseUrl, (state) => state.last_action === "preview" && state.last_status === "success", "Git to HA preview");
  assert(previewState.last_preview_paths.length > 0, "Preview returned no reviewed paths");
  await page.reload();
  await page.locator("ha-ops-preview-file").first().waitFor({ state: "attached" });
  await inspectPage(page, "apply-preview-desktop");
  let releaseOldDiff;
  let oldDiffStarted;
  const oldDiffRequest = new Promise((resolve) => { oldDiffStarted = resolve; });
  const oldDiffGate = new Promise((resolve) => { releaseOldDiff = resolve; });
  await page.route("**/diff-get?*", async (route) => {
    oldDiffStarted();
    await oldDiffGate;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, diff: "STALE PREVIEW DIFF" }) });
  });
  await page.locator("ha-ops-preview-file").first().evaluate((file) => { file.__diffRequest = file.setExpanded(true); });
  await oldDiffRequest;
  await page.locator("ha-ops-preview-file").first().evaluate(async (file) => {
    file.cursor = { ...file.cursor, artifact: "replacement-preview" };
    await file.updateComplete;
  });
  releaseOldDiff();
  const stalePreview = await page.locator("ha-ops-preview-file").first().evaluate(async (file) => {
    await file.__diffRequest;
    return { diff: file.diff, state: file.diffState, expanded: file.expanded };
  });
  assert(stalePreview.diff === "" && stalePreview.state !== "loaded" && !stalePreview.expanded,
    `Late preview diff reopened a stale panel: ${JSON.stringify(stalePreview)}`);
  await page.unroute("**/diff-get?*");

  let releaseOldConflict;
  let oldConflictStarted;
  const oldConflictRequest = new Promise((resolve) => { oldConflictStarted = resolve; });
  const oldConflictGate = new Promise((resolve) => { releaseOldConflict = resolve; });
  await page.route("**/conflict-diff-get?*", async (route) => {
    oldConflictStarted();
    await oldConflictGate;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, generation: 50, diff: "STALE CONFLICT DIFF" }) });
  });
  await page.locator("ha-ops-app").evaluate((app) => {
    app.state = { ...app.state, operation_generation: 50, conflicts: ["homeassistant/configuration.yaml"] };
    app.reconcileSelections();
    app.__conflictDiffRequest = app.loadConflictDiff("homeassistant/configuration.yaml").catch(() => {});
  });
  await oldConflictRequest;
  await page.locator("ha-ops-app").evaluate((app) => {
    app.state = { ...app.state, operation_generation: 51, conflicts: ["homeassistant/configuration.yaml"] };
    app.reconcileSelections();
  });
  releaseOldConflict();
  const staleConflict = await page.locator("ha-ops-app").evaluate(async (app) => {
    await app.__conflictDiffRequest;
    return app.conflictDiffs.has("homeassistant/configuration.yaml");
  });
  assert(!staleConflict, "Late conflict diff enabled a stale choice");
  const conflictControls = await page.locator("ha-ops-app").evaluate(async (app) => {
    app.state = { ...app.state, active_operation: null, conflicts: ["homeassistant/configuration.yaml"] };
    app.reconcileSelections();
    await app.updateComplete;
    const panel = app.renderRoot.querySelector('[data-testid="git-conflicts"]');
    const buttons = [...(panel?.querySelectorAll("vaadin-button") || [])];
    return { blocked: app.mutationBlocked(), count: buttons.length, disabled: buttons.every((button) => button.disabled) };
  });
  assert(!conflictControls.blocked && conflictControls.count >= 2 && conflictControls.disabled,
    `Stale conflict choices are available: ${JSON.stringify(conflictControls)}`);
  await page.unroute("**/conflict-diff-get?*");
  await secondTab.reload();
  await secondTab.getByTestId("reactive-previews").waitFor({ state: "attached" });
  assert((await stateAt(baseUrl)).last_preview_fingerprint === previewState.last_preview_fingerprint, "Second tab lost the server preview");

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const phone = await mobile.newPage();
  phone.on("pageerror", (error) => pageErrors.push(error.message));
  await phone.goto(baseUrl);
  await inspectPage(phone, "apply-preview-mobile");
  await phone.getByText("Change List").waitFor();
  await exerciseWorkflow(page, baseUrl, "desktop");
  await exerciseWorkflow(phone, baseUrl, "phone");
  const fallback = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await fallback.addInitScript(() => { Object.defineProperty(window, "WebSocket", { value: undefined }); });
  const fallbackPage = await fallback.newPage();
  await fallbackPage.goto(baseUrl);
  await fallbackPage.waitForFunction(() => document.querySelector("ha-ops-app")?.connection === "http");
  await fallbackPage.getByRole("button", { name: "Preview Git to HA" }).click();
  await waitForState(baseUrl, (state) => state.last_action === "preview" && state.last_status === "success", "HTTP fallback Preview");
  await fallbackPage.screenshot({ path: path.join(artifactsDir, "http-fallback-phone.png"), fullPage: true });
  assert(pageErrors.length === 0, `Browser errors: ${pageErrors.join("; ")}`);
  console.log(JSON.stringify({ ok: true, screenshots: artifactsDir }));
} finally {
  if (browser) await browser.close();
  child.kill("SIGTERM");
}
