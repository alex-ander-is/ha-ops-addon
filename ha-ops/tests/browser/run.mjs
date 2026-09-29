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
  await page.getByTestId("reactive-previews").waitFor({ state: "attached" });
  await inspectPage(page, "apply-preview-desktop");
  await secondTab.reload();
  await secondTab.getByTestId("reactive-previews").waitFor({ state: "attached" });
  assert((await stateAt(baseUrl)).last_preview_fingerprint === previewState.last_preview_fingerprint, "Second tab lost the server preview");

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const phone = await mobile.newPage();
  phone.on("pageerror", (error) => pageErrors.push(error.message));
  await phone.goto(baseUrl);
  await inspectPage(phone, "apply-preview-mobile");
  await phone.getByText("Change List").waitFor();
  assert(pageErrors.length === 0, `Browser errors: ${pageErrors.join("; ")}`);
  console.log(JSON.stringify({ ok: true, screenshots: artifactsDir }));
} finally {
  if (browser) await browser.close();
  child.kill("SIGTERM");
}
