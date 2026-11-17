import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync, realpathSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const directLayoutOnly = process.argv.includes("--direct-layout-only");
if (directLayoutOnly && (!process.env.HA_OPS_BROWSER_CDP_URL || !process.env.HA_OPS_BROWSER_ARTIFACTS_DIR)) {
  throw new Error("Direct layout checks require HA_OPS_BROWSER_CDP_URL and an external HA_OPS_BROWSER_ARTIFACTS_DIR; browser startup is not owned by this mode.");
}

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
  const root = mkdtempSync(path.join(os.tmpdir(), "ha-ops-dev-harness-"));
  console.log(JSON.stringify({ ownedHarnessRoot: root }));
  const child = spawn("python3", [path.join(appRoot, "dev_harness.py"), "--root", root, "--port", "0", "--print-json"], {
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
  return { child, ready, root };
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
  const state = await stateAt(baseUrl);
  throw new Error(`Timed out waiting for ${label}: ${JSON.stringify({
    last_action: state.last_action, last_status: state.last_status, last_message: state.last_message,
  })}`);
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
  const layout = await page.locator("ha-ops-app").evaluate((app) => {
    const root = app.renderRoot;
    const controls = root.querySelector(".control-card");
    const details = root.querySelector(".details-card");
    const log = root.querySelector("ha-ops-log").renderRoot.querySelector("pre");
    return {
      sameRow: Math.abs(controls.getBoundingClientRect().top - details.getBoundingClientRect().top) < 2,
      controlHeight: controls.getBoundingClientRect().height,
      detailsHeight: details.getBoundingClientRect().height,
      logHeight: log.clientHeight,
      inlineHeight: details.style.getPropertyValue("--details-card-height"),
      hasObserver: Boolean(app.resizeObserver),
    };
  });
  if (layout.sameRow) {
    assert(Math.abs(layout.controlHeight - layout.detailsHeight) < 2,
      `${label}: log card does not fill control card: ${JSON.stringify(layout)}`);
    assert(layout.logHeight > layout.detailsHeight - 100,
      `${label}: log viewport is shorter than its card: ${JSON.stringify(layout)}`);
  }
  await page.screenshot({ path: path.join(artifactsDir, `${label}.png`), fullPage: true });
}

async function assertLogUsesAvailableHeight(page) {
  const metrics = await page.locator("ha-ops-app").evaluate(async (app) => {
    const original = app.state;
    const log = app.querySelector("ha-ops-log").renderRoot.querySelector("pre");
    const lineHeight = Number.parseFloat(getComputedStyle(log).fontSize) * 1.2;
    const count = Math.floor((log.clientHeight - 60) / lineHeight);
    app.state = { ...original, last_details: Array.from({ length: count }, (_, index) => `Log line ${index}`), last_message: "" };
    await app.updateComplete;
    const result = { lineCount: count, clientHeight: log.clientHeight, scrollHeight: log.scrollHeight };
    app.state = original;
    await app.updateComplete;
    return result;
  });
  assert(metrics.lineCount > 20 && metrics.scrollHeight <= metrics.clientHeight + 2,
    `Log scrolls before filling its available space: ${JSON.stringify(metrics)}`);
}

async function exerciseManagedTargets(page, secondTab, baseUrl, label) {
  const section = page.getByTestId("managed-targets-section");
  const details = section.locator("vaadin-details");
  const table = section.locator(".managed-targets-table");
  await section.getByRole("heading", { name: "Managed Targets" }).waitFor();
  await section.getByText("Configured targets come from the manifest.", { exact: false }).waitFor();
  assert((await section.textContent()).includes("both Save HA to Git and Apply Git to HA"), `${label}: selection effect not explained`);
  assert((await section.textContent()).includes("does not immediately change live Home Assistant"), `${label}: live effect not explained`);
  assert(!(await details.evaluate((node) => node.opened)) && !(await table.isVisible()), `${label}: table starts expanded`);
  await details.locator("vaadin-details-summary").click();
  assert((await details.evaluate((node) => node.opened)) && await table.isVisible(), `${label}: table did not open`);
  const checkbox = section.locator('vaadin-checkbox[aria-label="Managed Local MQTT"]');
  await checkbox.waitFor();
  const before = await checkbox.evaluate((node) => node.checked);
  const previousAction = (await stateAt(baseUrl)).last_action;
  await checkbox.click();
  await page.waitForFunction((wasChecked) =>
    document.querySelector("ha-ops-app")?.view.selected_addons.includes("local_mqtt") !== wasChecked, before);
  const selectedView = (await (await fetch(`${baseUrl}api/v1/state`)).json()).view;
  assert(selectedView.selected_addons.includes("local_mqtt") !== before, `${label}: App selection was not saved`);
  assert((await stateAt(baseUrl)).last_action === previousAction, `${label}: App selection started a live action`);
  assert((await checkbox.evaluate((node) => node.checked)) !== before, `${label}: checkbox did not update`);
  assert(await details.evaluate((node) => node.opened), `${label}: selection collapsed the table`);
  await page.locator("ha-ops-app").evaluate(async (app) => { app.uncertainCommandId = "fixture"; await app.updateComplete; });
  assert(await checkbox.evaluate((node) => node.disabled), `${label}: checkbox enabled while commands are blocked`);
  await page.locator("ha-ops-app").evaluate(async (app) => { app.uncertainCommandId = null; await app.updateComplete; });
  await details.locator("vaadin-details-summary").click();
  assert(!(await details.evaluate((node) => node.opened)) && !(await table.isVisible()), `${label}: table did not collapse`);
  await details.locator("vaadin-details-summary").click();
  await page.reload();
  await section.getByRole("heading", { name: "Managed Targets" }).waitFor();
  assert(!(await details.evaluate((node) => node.opened)) && !(await table.isVisible()), `${label}: reload retained expansion`);
  assert((await section.getByText("Configured targets come from the manifest.", { exact: false }).isVisible()), `${label}: explanation disappeared on reload`);
  if (secondTab) {
    const otherDetails = secondTab.getByTestId("managed-targets-section").locator("vaadin-details");
    assert(!(await otherDetails.evaluate((node) => node.opened)), `${label}: second tab inherited expansion`);
  }
  await page.screenshot({ path: path.join(artifactsDir, `${label}-managed-targets-collapsed.png`), fullPage: true });
  await details.locator("vaadin-details-summary").click();
  await page.screenshot({ path: path.join(artifactsDir, `${label}-managed-targets-expanded.png`), fullPage: true });
  if ((await checkbox.evaluate((node) => node.checked)) !== before) {
    await checkbox.click();
    await page.waitForFunction((wasChecked) =>
      document.querySelector("ha-ops-app")?.view.selected_addons.includes("local_mqtt") === wasChecked, before);
  }
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
  await page.locator("ha-ops-app").evaluate((app) => {
    const receive = app.receive.bind(app);
    app.receive = (frame) => { if (frame.type !== "state_patch") receive(frame); };
  });
  await page.getByRole("button", { name: "Preview Git to HA" }).click();
  await waitForState(baseUrl, (state) => state.last_action === "preview" && state.last_status === "success", `${label} Apply preview`);
  await page.waitForFunction(() => document.querySelector("ha-ops-app")?.state.last_action === "preview" &&
    document.querySelector("ha-ops-app")?.state.last_status === "success", undefined, { timeout: 5000 });
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

  const seed = await fetch(`${baseUrl}__dev_harness__/seed-internal-ids`, { method: "POST" });
  assert(seed.ok, `${label} Internal IDs fixture could not be seeded`);
  await page.reload();
  await page.getByRole("button", { name: "Check actions IDs" }).click();
  const idsPreview = await waitForState(baseUrl, (state) => state.last_action === "internal_ids_preview" &&
    state.last_status === "success" && state.last_internal_ids_rows?.some((row) => row.changes > 0), `${label} Internal IDs preview`);
  const row = idsPreview.last_internal_ids_rows.find((item) => item.changes > 0);
  assert(row.diff_sha256 && !row.diff, `${label} exact diff must be fetched separately`);
  await page.reload();
  const section = page.getByTestId("internal-ids-preview-section");
  const migrate = section.getByRole("button", { name: "Migrate and Save" });
  assert(await migrate.isDisabled(), `${label} migration enabled before exact diff review`);
  await section.locator("vaadin-details").first().click();
  await section.getByText("switch.fixture", { exact: false }).waitFor();
  const loaded = await page.locator("ha-ops-app").evaluate((app, path) => app.internalDiffs.get(path), row.path);
  assert(loaded?.includes("switch.fixture"), `${label} exact diff was not loaded from server`);
  await page.screenshot({ path: path.join(artifactsDir, `${label}-internal-ids-diff.png`), fullPage: true });
  const checkbox = section.locator(`vaadin-checkbox[aria-label="Migrate ${row.path}"]`);
  if (row.selected) {
    await checkbox.click();
    await waitForState(baseUrl, (state) => state.last_internal_ids_rows?.some((item) => item.path === row.path && !item.selected), `${label} Internal IDs deselection`);
  }
  await checkbox.click();
  await waitForState(baseUrl, (state) => state.last_internal_ids_rows?.some((item) => item.path === row.path && item.selected), `${label} Internal IDs selection`);
  await migrate.click();
  await page.locator("vaadin-confirm-dialog[opened]").getByRole("button", { name: "Confirm" }).click();
  await waitForState(baseUrl, (state) => state.last_action === "internal_ids_migrate" && state.last_status === "success", `${label} Internal IDs migration`);

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

const profileDir = path.join(sharedRoot, "user-data/google-chrome");
const ownedPages = new Map();
const launchedContexts = new WeakSet();
async function openOwnedPage(context, viewport = { width: 1600, height: 1000 }) {
  const page = (launchedContexts.has(context) && context.pages().find((candidate) => candidate.url() === "about:blank" && !ownedPages.has(candidate)))
    || await context.newPage();
  const session = await context.newCDPSession(page);
  const { targetInfo } = await session.send("Target.getTargetInfo");
  await session.detach();
  ownedPages.set(page, targetInfo.targetId);
  console.log(JSON.stringify({ ownedTab: targetInfo.targetId }));
  await page.setViewportSize(viewport);
  return page;
}
async function openPersistentBrowser() {
  if (process.env.HA_OPS_BROWSER_CDP_URL) {
    const browser = await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL);
    return browser.contexts()[0];
  }
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: "chrome", headless: false, args: ["--profile-directory=Default"],
  });
  launchedContexts.add(context);
  return context;
}
async function cleanupOwned(context, child, root) {
  console.log("Closing only recorded test-owned tabs: their checks are finished; existing tabs and persistent sessions are preserved.");
  for (const [page, targetId] of ownedPages) {
    if (!page.isClosed()) await page.close();
    console.log(JSON.stringify({ closedOwnedTab: targetId }));
  }
  ownedPages.clear();
  if (context && launchedContexts.has(context) && context.pages().length === 0) await context.close();
  else if (context) console.log(JSON.stringify({ retainedTabs: context.pages().length }));
  child.kill("SIGTERM");
  await new Promise((resolve) => { if (child.exitCode !== null) resolve(); else child.once("exit", resolve); });
  if (root) {
    const exactRoot = realpathSync(root);
    assert(/^ha-ops-dev-harness-/.test(path.basename(exactRoot)) && exactRoot.startsWith(realpathSync(os.tmpdir()) + path.sep),
      "Refusing cleanup outside owned disposable harness root");
    rmSync(exactRoot, { recursive: true });
  }
}

async function exerciseGitAccess(page, baseUrl, root, label) {
  const section = page.locator("section").filter({ has: page.getByRole("heading", { name: "Git Access", exact: true }) });
  const optionsPath = path.join(root, "data/options.json");
  const keyPath = path.join(root, "data/work/generated_deploy_key");
  const pubPath = keyPath + ".pub";
  const initialOptions = JSON.parse(readFileSync(optionsPath, "utf8"));
  const publicText = `ssh-ed25519 ${"A".repeat(600)} <img src=x onerror=alert(1)>&comment`;
  const evidence = [];
  const refresh = async () => {
    await page.locator("ha-ops-app").evaluate(async (app) => {
      app.applyBaseline(await (await fetch("api/v1/state")).json());
      await app.updateComplete;
    });
  };
  const setFixture = (mode, repoUrl) => {
    writeFileSync(optionsPath, JSON.stringify({ ...initialOptions, repo_url: repoUrl,
      git_ssh_key: mode === "manual" ? "MANUAL_PRIVATE_SENTINEL" : "" }));
    for (const file of [keyPath, pubPath]) rmSync(file, { force: true });
    if (mode !== "none") {
      writeFileSync(keyPath, "GENERATED_PRIVATE_SENTINEL");
      writeFileSync(pubPath, publicText + "\n");
    }
  };
  await page.goto(baseUrl);
  await section.waitFor();
  await page.waitForFunction(() => ["connected", "http"].includes(document.querySelector("ha-ops-app")?.connection)
    && !document.querySelector("ha-ops-app")?.replayPending);
  const statusCopy = {
    manual: "Using the private key from git_ssh_key in App configuration.",
    generated: "Using the deploy key generated and stored inside HA Ops.",
    none: "No SSH key is configured yet.",
  };
  const generatedHint = "Add this public key to GitHub as a Deploy Key with write access for ha-config.";
  const nonSshHint = "Your repository URL is not SSH-based, so a deploy key may not be needed.";
  const sshHint = "Click Generate Deploy Key, then paste the public key into GitHub Deploy Keys.";
  for (const mode of ["none", "generated", "manual"]) {
    for (const repoUrl of ["git@example.invalid:repo", "ssh://example.invalid/repo", "https://example.invalid/repo", ""]) {
      setFixture(mode, repoUrl);
      await refresh();
      const actual = await section.evaluate((node) => ({
        children: [...node.children].map((item) => ({ tag: item.tagName, text: item.textContent.trim() })),
        text: node.textContent, key: node.querySelector("pre")?.textContent,
        rawControls: node.querySelectorAll("button,form,textarea").length,
        insertedImages: node.querySelectorAll("img,script").length,
        selectable: node.querySelector("pre") ? getComputedStyle(node.querySelector("pre")).userSelect : null,
      }));
      const ssh = repoUrl.startsWith("git@") || repoUrl.startsWith("ssh://");
      const expected = ["Git Access", statusCopy[mode]];
      if (!ssh) expected.push(nonSshHint);
      else if (mode === "none") expected.push(sshHint);
      expected.push(mode === "generated" ? "Regenerate Deploy Key" : "Generate Deploy Key");
      if (mode === "generated") expected.push(generatedHint, publicText);
      assert(JSON.stringify(actual.children.map((item) => item.text)) === JSON.stringify(expected),
        `${label} ${mode}/${repoUrl}: historical content/order missing: ${JSON.stringify(actual.children)}`);
      assert(actual.rawControls === 0 && actual.insertedImages === 0, `${label}: unsafe/legacy Git Access markup`);
      assert(!actual.text.includes("PRIVATE_SENTINEL"), `${label}: private fixture leaked`);
      assert(mode === "generated" ? actual.key === publicText && actual.selectable !== "none" : actual.key === undefined,
        `${label}: key visibility or selectable literal text wrong`);
      evidence.push({ mode, repoUrl, children: actual.children });
    }
  }

  setFixture("generated", initialOptions.repo_url);
  await refresh();
  await page.screenshot({ path: path.join(artifactsDir, `${label}-git-access-desktop.png`), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await section.evaluate((node) => ({
    pageOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
    keyOverflow: node.querySelector("pre").scrollWidth > node.querySelector("pre").clientWidth + 2,
    whiteSpace: getComputedStyle(node.querySelector("pre")).whiteSpace,
  }));
  assert(!overflow.pageOverflow && !overflow.keyOverflow && overflow.whiteSpace === "pre-wrap",
    `${label}: mobile public key overflow: ${JSON.stringify(overflow)}`);
  await page.screenshot({ path: path.join(artifactsDir, `${label}-git-access-mobile.png`), fullPage: true });

  const fences = await page.locator("ha-ops-app").evaluate(async (app) => {
    const original = { state: app.state, connection: app.connection, replayPending: app.replayPending,
      commandIntent: app.commandIntent, acceptedCommandId: app.acceptedCommandId,
      uncertainCommandId: app.uncertainCommandId, versionMismatchOpen: app.versionMismatchOpen };
    const transport = window.fetch;
    const socketSend = app.socket?.send;
    let sends = 0;
    window.fetch = async () => { sends++; throw new Error("Unexpected fenced transport"); };
    if (app.socket) app.socket.send = () => { sends++; };
    const cases = [
      ["intent", { commandIntent: { id: "fixture" } }], ["accepted", { acceptedCommandId: "fixture" }],
      ["uncertain", { uncertainCommandId: "fixture" }], ["replay", { replayPending: true }],
      ...["connecting", "replaying", "reconnecting", "unknown", "closed"].map((connection) => [connection, { connection }]),
      ["running", { state: { ...original.state, last_status: "running" } }],
      ["command", { state: { ...original.state, command_records: { fixture: { status: "accepted" } } } }],
      ["operation", { state: { ...original.state, active_operation: { command: "save", phase: "recovery_required" } } }],
      ["deleted recovery", { state: { ...original.state, deleted_devices_recovery_phase: "recovery_required" } }],
      ["Docker", { state: { ...original.state, docker_build_cache_prune_fence: true } }],
      ["pending", { state: { ...original.state, deleted_devices_pending_confirmation: true } }],
      ["Save retry", { state: { ...original.state, save_push_retry_pending: true } }],
      ["version", { versionMismatchOpen: true }],
    ];
    const results = [];
    try {
      for (const [name, values] of cases) {
        Object.assign(app, original);
        app.requestUpdate();
        await app.updateComplete;
        const oldButton = app.querySelector('[data-testid="git-access-section"] vaadin-button');
        Object.assign(app, values);
        // Synthetic activation before rendering must still hit the handler fence.
        oldButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        await app.dispatchCommand("generate_key", new URL("generate-key", location.href).href);
        app.requestUpdate();
        await app.updateComplete;
        const button = app.querySelector('[data-testid="git-access-section"] vaadin-button');
        const style = getComputedStyle(button);
        results.push({ name, disabled: button.disabled, sends, background: style.backgroundColor,
          color: style.color, border: style.borderTopColor });
      }
      Object.assign(app, original);
      app.versionMismatchOpen = true;
      app.state = { ...original.state, deleted_devices_pending_confirmation: true };
      app.acknowledgeVersionMismatch();
      app.requestUpdate();
      await app.updateComplete;
      const pendingAfterAck = app.querySelector('[data-testid="git-access-section"] vaadin-button').disabled;
      app.state = original.state;
      app.requestUpdate();
      await app.updateComplete;
      const enabledAfterAck = !app.querySelector('[data-testid="git-access-section"] vaadin-button').disabled;
      if (!pendingAfterAck || !enabledAfterAck) throw new Error("Version acknowledgement bypassed remaining Git Access fence");
    } finally {
      window.fetch = transport;
      if (app.socket) app.socket.send = socketSend;
      Object.assign(app, original);
      app.requestUpdate();
      await app.updateComplete;
    }
    return results;
  });
  for (const result of fences) assert(result.disabled && result.sends === 0
    && result.background === "rgb(229, 231, 235)" && result.color === "rgb(107, 114, 128)"
    && result.border === "rgb(209, 213, 219)", `${label} fence failed: ${JSON.stringify(result)}`);

  let generationPosts = [];
  const observe = (request) => {
    if (request.method() === "POST" && request.url().endsWith("/generate-key")) generationPosts.push(request.postDataJSON());
  };
  page.on("request", observe);
  setFixture("none", initialOptions.repo_url);
  await refresh();
  const rotate = async (expectedCount) => {
    await section.locator("vaadin-button").evaluate((button) => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await page.waitForFunction(() => {
      const app = document.querySelector("ha-ops-app");
      return app?.view.git_access?.mode === "generated" && !app.commandIntent && !app.acceptedCommandId;
    });
    assert(generationPosts.length === expectedCount && generationPosts.at(-1).command === "generate_key",
      `${label}: rapid generation was not one HTTP command`);
    return section.locator("pre").textContent();
  };
  const firstKey = await rotate(1);
  assert(firstKey.startsWith("ssh-ed25519 ") && await section.getByRole("button", { name: "Regenerate Deploy Key" }).isVisible(),
    `${label}: Generate did not restore generated card`);
  const secondKey = await rotate(2);
  assert(secondKey !== firstKey, `${label}: Regenerate retained old key`);
  await page.reload();
  await section.locator("pre").waitFor();
  assert(await section.locator("pre").textContent() === secondKey && generationPosts.length === 2,
    `${label}: reload lost authoritative key or generated automatically`);
  writeFileSync(optionsPath, JSON.stringify({ ...initialOptions, git_ssh_key: "MANUAL_PRIVATE_SENTINEL" }));
  await refresh();
  await section.getByRole("button", { name: "Generate Deploy Key", exact: true }).click();
  await page.waitForFunction(() => {
    const app = document.querySelector("ha-ops-app");
    return !app?.commandIntent && !app?.acceptedCommandId;
  });
  assert(await section.locator("pre").count() === 0 && (await section.textContent()).includes(statusCopy.manual),
    `${label}: Generate incorrectly replaced manual precedence`);
  setFixture("none", initialOptions.repo_url);
  await refresh();
  assert(await section.locator("pre").count() === 0, `${label}: removed pair retained stale key`);
  const failures = [];
  await page.route("**/generate-key", (route) => route.fulfill({ status: 409, contentType: "application/json",
    body: JSON.stringify({ ok: false, message: "Fixture definitive refusal" }) }));
  await section.locator("vaadin-button").click();
  await page.waitForFunction(() => {
    const app = document.querySelector("ha-ops-app");
    return !app.commandIntent && !app.acceptedCommandId && !app.uncertainCommandId;
  });
  assert(await section.locator("pre").count() === 0 && generationPosts.length === 4,
    `${label}: definitive refusal invented key or retried`);
  failures.push({ refusalPosts: generationPosts.length });
  await page.unroute("**/generate-key");
  await page.locator("ha-ops-app").evaluate((app) => {
    app.__originalReceive = app.receive;
    app.receive = (frame) => { if (frame.type !== "state_patch") app.__originalReceive(frame); };
  });
  await page.route("**/generate-key", async (route) => {
    await route.fetch(); // The disposable server committed the effect; only its response is lost.
    await route.abort("failed");
  });
  await section.locator("vaadin-button").click();
  await page.waitForFunction(() => document.querySelector("ha-ops-app")?.uncertainCommandId);
  const uncertain = await section.locator("vaadin-button").evaluate((button) => button.disabled);
  assert(uncertain && generationPosts.length === 5 && await section.locator("pre").count() === 0,
    `${label}: lost response retried or invented public text`);
  await new Promise((resolve) => setTimeout(resolve, 250));
  assert(generationPosts.length === 5, `${label}: lost response automatically rotated again`);
  failures.push({ lostResponsePosts: generationPosts.length, uncertain });
  await page.unroute("**/generate-key");
  await page.locator("ha-ops-app").evaluate((app) => { app.receive = app.__originalReceive; delete app.__originalReceive; });
  await refresh();
  assert(await section.locator("pre").count() === 1 && await section.locator("pre").textContent() === readFileSync(pubPath, "utf8").trim(),
    `${label}: explicit refresh did not reconcile committed generation`);
  setFixture("none", initialOptions.repo_url);
  await refresh();
  page.off("request", observe);
  writeFileSync(path.join(artifactsDir, `${label}-git-access-dom.json`), JSON.stringify({ evidence, overflow, fences, failures,
    generationPosts: generationPosts.map(({ command, command_id, generation }) => ({ command, command_id, generation })) }, null, 2));
}

async function runGitAccessOnly() {
  const { child, ready, root } = await startHarness();
  let context;
  try {
    const fixture = await ready;
    assert(realpathSync(root) === realpathSync(fixture.root), "Unexpected harness root");
    context = await openPersistentBrowser();
    console.log(JSON.stringify({ initialTabs: context.pages().length }));
    const page = await openOwnedPage(context);
    await exerciseGitAccess(page, fixture.baseUrl, root, "connected");
    const fallback = await openOwnedPage(context);
    await fallback.addInitScript(() => { Object.defineProperty(window, "WebSocket", { value: undefined }); });
    await exerciseGitAccess(fallback, fixture.baseUrl, root, "http");
    console.log(JSON.stringify({ ok: true, screenshots: artifactsDir }));
  } finally { await cleanupOwned(context, child, root); }
}

async function runDirectLayoutOnly() {
  const { child, ready, root } = await startHarness();
  let context, cdpBrowser;
  const evidence = { viewports: [], pageErrors: [], loadingCount: null };
  const reportPath = path.join(artifactsDir, "direct-layout-dom.json");
  try {
    const fixture = await ready;
    assert(realpathSync(root) === realpathSync(fixture.root), "Unexpected harness root");
    context = await openPersistentBrowser();
    cdpBrowser = context.browser();
    const page = await openOwnedPage(context);
    page.on("pageerror", error => evidence.pageErrors.push(error.message));
    if (process.env.HA_OPS_LAYOUT_BASELINE_BUNDLE) {
      await page.route("**/ha-ops.js", route => route.fulfill({ contentType: "text/javascript", body: readFileSync(process.env.HA_OPS_LAYOUT_BASELINE_BUNDLE) }));
    }
    await page.goto(fixture.baseUrl);
    await page.getByTestId("git-access-section").waitFor();
    await page.getByTestId("managed-targets-section").waitFor();
    await page.locator("ha-ops-direct").evaluate(async component => { await component.updateComplete; });
    for (const width of [1440, 1920, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      const metrics = await page.locator("ha-ops-app").evaluate(async app => {
        await app.updateComplete;
        const root = app.renderRoot;
        const components = [...root.querySelectorAll("ha-ops-direct")];
        const direct = components[0];
        await direct.updateComplete;
        const section = direct.renderRoot.querySelector("section");
        const git = root.querySelector('[data-testid="git-access-section"]');
        const managed = root.querySelector('[data-testid="managed-targets-section"]');
        const rect = node => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width }; };
        return { count: components.length, section: rect(section), git: rect(git), managed: rect(managed),
          ordered: Boolean(git.compareDocumentPosition(direct) & Node.DOCUMENT_POSITION_FOLLOWING) && Boolean(direct.compareDocumentPosition(managed) & Node.DOCUMENT_POSITION_FOLLOWING),
          directChild: direct.parentElement === git.parentElement && direct.parentElement === managed.parentElement,
          inHeader: Boolean(root.querySelector(".top-grid ha-ops-direct")),
          documentOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
          sectionOverflow: section.scrollWidth > section.clientWidth + 2 };
      });
      evidence.viewports.push({ width, ...metrics });
      writeFileSync(reportPath, JSON.stringify(evidence, null, 2));
      assert(metrics.count === 1 && metrics.ordered && metrics.directChild && !metrics.inHeader, `${width}: direct setup order/count: ${JSON.stringify(metrics)}`);
      assert(metrics.section.top >= metrics.git.bottom && metrics.managed.top >= metrics.section.bottom, `${width}: direct setup vertical order`);
      for (const neighbor of [metrics.git, metrics.managed]) {
        assert(Math.abs(metrics.section.left - neighbor.left) <= 2 && Math.abs(metrics.section.right - neighbor.right) <= 2, `${width}: inner setup edges differ: ${JSON.stringify(metrics)}`);
      }
      assert(!metrics.documentOverflow && !metrics.sectionOverflow, `${width}: horizontal overflow: ${JSON.stringify(metrics)}`);
    }
    evidence.loadingCount = await page.locator("ha-ops-app").evaluate(async app => {
      const original = app.view;
      try {
        app.view = {};
        await app.updateComplete;
        return app.renderRoot.querySelectorAll("ha-ops-direct").length;
      } finally { app.view = original; await app.updateComplete; }
    });
    assert(evidence.loadingCount === 0, "Loading view flashed direct setup");
    assert(await page.locator("ha-ops-direct").count() === 1, "Restored view duplicated direct setup");
    assert(evidence.pageErrors.length === 0, `Page errors: ${evidence.pageErrors.join(" | ")}`);
    evidence.ok = true;
    console.log(JSON.stringify({ ok: true, domEvidence: reportPath }));
  } finally {
    try {
      writeFileSync(reportPath, JSON.stringify(evidence, null, 2));
    } finally {
      try { await cleanupOwned(context, child, root); }
      finally {
        // For connectOverCDP this disconnects our client, preserving the shared browser.
        await cdpBrowser?.close();
      }
    }
  }
}

if (directLayoutOnly) {
  await runDirectLayoutOnly();
} else if (process.argv.includes("--git-access-only")) {
  await runGitAccessOnly();
} else {
const { child, ready, root: fixtureRoot } = await startHarness();
let browser;
try {
  const { baseUrl, root } = await ready;
  assert(realpathSync(fixtureRoot) === realpathSync(root), "Unexpected harness root");
  browser = await openPersistentBrowser();
  const page = await openOwnedPage(browser);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => { if (response.status() >= 400) pageErrors.push(`${response.status()} ${response.url()}`); });
  await page.goto(baseUrl);
  await inspectPage(page, "initial-desktop");
  await assertLogUsesAvailableHeight(page);
  const staleBaselineRejected = await page.locator("ha-ops-app").evaluate(async (app) => {
    const response = await fetch("api/v1/state");
    const old = await response.json();
    const newer = { ...old, revision: Number(old.state.state_revision) + 1,
      state: { ...old.state, last_message: "newer websocket state" } };
    app.applyBaseline(newer);
    const accepted = app.applyBaseline({ ...old, state: { ...old.state, last_message: "stale HTTP state" } });
    return !accepted && app.state.last_message === "newer websocket state" && app.revision === newer.revision;
  });
  assert(staleBaselineRejected, "An older HTTP baseline replaced a newer state");
  assert((await stateAt(baseUrl)).last_status !== "error", "Initial state is in error");

  const secondTab = await openOwnedPage(browser);
  await secondTab.goto(baseUrl);
  await inspectPage(secondTab, "second-tab-desktop");
  await exerciseManagedTargets(page, secondTab, baseUrl, "desktop");
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

  const phone = await openOwnedPage(browser, { width: 390, height: 844 });
  phone.on("pageerror", (error) => pageErrors.push(error.message));
  await phone.goto(baseUrl);
  await inspectPage(phone, "apply-preview-mobile");
  await exerciseManagedTargets(phone, null, baseUrl, "phone");
  await phone.getByText("Change List").waitFor();
  await exerciseWorkflow(page, baseUrl, "desktop");
  await exerciseWorkflow(phone, baseUrl, "phone");
  const fallbackPage = await openOwnedPage(browser, { width: 390, height: 844 });
  await fallbackPage.addInitScript(() => { Object.defineProperty(window, "WebSocket", { value: undefined }); });
  await fallbackPage.goto(baseUrl);
  await fallbackPage.waitForFunction(() => document.querySelector("ha-ops-app")?.connection === "http");
  await fallbackPage.getByRole("button", { name: "Preview Git to HA" }).click();
  await waitForState(baseUrl, (state) => state.last_action === "preview" && state.last_status === "success", "HTTP fallback Preview");
  await fallbackPage.screenshot({ path: path.join(artifactsDir, "http-fallback-phone.png"), fullPage: true });
  assert(pageErrors.length === 0, `Browser errors: ${pageErrors.join("; ")}`);
  console.log(JSON.stringify({ ok: true, screenshots: artifactsDir }));
} finally {
  await cleanupOwned(browser, child, fixtureRoot);
}

}
