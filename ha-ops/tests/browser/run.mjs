import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, "../..");
const repoRoot = path.resolve(appRoot, "..");
const sharedRoot = process.env.PLAYWRIGHT_SHARED_ROOT || "/Users/purportex/Applications/Playwright";
const runtime = await import(pathToFileURL(path.join(sharedRoot, "src/runtime.mjs")).href);
const { chromium } = runtime;
const PREVIEW_BUTTONS = ["Preview Git to HA", "Preview HA to Git"];

function startHarness() {
  const child = spawn(
    "python3",
    [path.join(appRoot, "dev_harness.py"), "--port", "0", "--print-json"],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        PYTHONPYCACHEPREFIX: process.env.PYTHONPYCACHEPREFIX || "/private/tmp/ha-ops-browser-pycache",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  let exited = false;
  const exitedPromise = new Promise((resolve) => {
    child.once("exit", (code, signal) => {
      exited = true;
      resolve({ code, signal });
    });
  });
  const ready = new Promise((resolve, reject) => {
    let stdout = "";
    let settled = false;
    const timer = setTimeout(() => reject(new Error(`dev harness did not start\n${stderr}`)), 10000);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
      const line = stdout.split(/\r?\n/).find((item) => item.trim().startsWith("{"));
      if (!line || settled) {
        return;
      }
      clearTimeout(timer);
      try {
        settled = true;
        resolve(JSON.parse(line));
      } catch (error) {
        settled = true;
        reject(error);
      }
    });
    exitedPromise.then(({ code, signal }) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`dev harness exited before ready with ${code ?? signal}\n${stderr}`));
      }
    });
  });
  child.haOpsExited = () => exited;
  child.haOpsExitedPromise = exitedPromise;
  child.haOpsStderr = () => stderr;
  return { child, ready };
}

async function stopHarness(child) {
  if (!child || child.haOpsExited?.()) {
    return;
  }
  const exitedPromise =
    child.haOpsExitedPromise || new Promise((resolve) => child.once("exit", (code, signal) => resolve({ code, signal })));
  let timeoutId;
  const timeout = new Promise((resolve) => {
    timeoutId = setTimeout(() => resolve({ timedOut: true }), 5000);
  });
  child.kill("SIGTERM");
  const result = await Promise.race([exitedPromise, timeout]);
  clearTimeout(timeoutId);
  if (result?.timedOut) {
    throw new Error(`dev harness did not stop after SIGTERM\n${child.haOpsStderr?.() || ""}`);
  }
}

async function harnessPost(baseUrl, pathName, form = {}) {
  const response = await fetch(new URL(pathName, baseUrl), {
    method: "POST",
    body: new URLSearchParams(form),
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(`harness POST ${pathName} failed: ${JSON.stringify(payload)}`);
  }
  return payload;
}

async function diagnostics(baseUrl) {
  const response = await fetch(new URL("__dev_harness__/diagnostics", baseUrl));
  if (!response.ok) {
    throw new Error(`diagnostics failed: ${response.status}`);
  }
  return response.json();
}

async function waitFor(label, callback, timeout = 5000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    last = await callback();
    if (last) {
      return last;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`timeout waiting for ${label}; last=${JSON.stringify(last)}`);
}

async function runningDomSnapshot(page, buttonName) {
  const button = page.getByRole("button", { name: buttonName });
  return {
    loadId: await page.evaluate(() => window.__haOpsLoadId),
    status: await page.locator("[data-status-code]").getAttribute("data-status-code"),
    buttonDisabled: await button.isDisabled(),
    buttonText: await button.textContent(),
    details: await page.getByTestId("operation-log").textContent(),
  };
}

async function stateChangingControls(page) {
  return page.evaluate(() => {
    function controlLabel(control) {
      if (control.tagName === "BUTTON") {
        return (control.textContent || "").trim();
      }
      const label = control.closest("label");
      return (label?.innerText || control.getAttribute("aria-label") || control.name || control.value || "").trim();
    }

    return Array.from(document.querySelectorAll("form[method='post']"))
      .flatMap((form) => {
        const action = form.getAttribute("action") || "";
        return Array.from(form.querySelectorAll("vaadin-button, vaadin-checkbox, vaadin-radio-group, vaadin-select, input:not([type='hidden']), textarea"))
          .filter((control) => control.getClientRects().length > 0)
          .map((control, index) => {
            const type = (control.getAttribute("type") || "").toLowerCase();
            const label = controlLabel(control);
            return {
              action,
              disabled: Boolean(control.disabled),
              key: [
                action,
                control.tagName.toLowerCase(),
                type,
                control.getAttribute("name") || "",
                control.getAttribute("value") || "",
                label,
                index,
              ].join("|"),
              label: label || `${control.tagName.toLowerCase()} ${control.getAttribute("name") || ""}`.trim(),
            };
          });
      });
  });
}

async function assertAllStateChangingControlsDisabled(page, phase) {
  const deadline = Date.now() + 5000;
  let controls = [];
  while (Date.now() < deadline) {
    controls = await stateChangingControls(page);
    if (controls.length > 0 && controls.every((control) => control.disabled)) {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert(controls.length > 0, `${phase} did not expose any state-changing controls`);
  const enabled = controls.filter((control) => !control.disabled);
  assert(
    enabled.length === 0,
    `${phase} left state-changing controls enabled: ${enabled.map((control) => `${control.action}:${control.label}`).join(", ")}`,
  );
}

async function assertPreviouslyEnabledControlsRestored(page, beforeControls, phase) {
  const beforeEnabled = new Map(beforeControls.filter((control) => !control.disabled).map((control) => [control.key, control]));
  assert(beforeEnabled.size > 0, `${phase} had no initially enabled controls to restore`);
  const afterByKey = new Map((await stateChangingControls(page)).map((control) => [control.key, control]));
  const stillDisabled = [];
  for (const [key, before] of beforeEnabled.entries()) {
    const after = afterByKey.get(key);
    if (after && after.disabled) {
      stillDisabled.push(`${before.action}:${before.label}`);
    }
  }
  assert(
    stillDisabled.length === 0,
    `${phase} did not restore previously enabled controls: ${stillDisabled.join(", ")}`,
  );
}

async function assertPreviewButtonState(page, buttonName, expectedDisabled, phase) {
  const button = page.getByRole("button", { name: buttonName });
  const match = await waitFor(`${buttonName} ${phase} disabled=${expectedDisabled}`, async () => {
    const value = await button.isDisabled();
    return value === expectedDisabled ? { value } : null;
  });
  const actual = match.value;
  assert(
    actual === expectedDisabled,
    `${buttonName} ${phase} disabled state was ${actual}, expected ${expectedDisabled}`,
  );
}

async function assertPreviewButtonsState(page, expectedDisabled, phase) {
  for (const buttonName of PREVIEW_BUTTONS) {
    await assertPreviewButtonState(page, buttonName, expectedDisabled, phase);
  }
}

async function assertStatus(page, expectedStatus, phase) {
  const actual = await page.locator("[data-status-code]").getAttribute("data-status-code");
  assert(actual === expectedStatus, `${phase} status was ${actual}, expected ${expectedStatus}`);
}

async function assertStatusNot(page, unexpectedStatus, phase) {
  const actual = await page.locator("[data-status-code]").getAttribute("data-status-code");
  assert(actual !== unexpectedStatus, `${phase} status was unexpectedly ${unexpectedStatus}`);
}

async function assertLogContains(page, expectedText, phase) {
  const logText = await page.getByTestId("operation-log").textContent();
  assert(
    (logText || "").includes(expectedText),
    `${phase} log did not contain ${expectedText}; log=${JSON.stringify(logText)}`,
  );
}

async function assertLogPanelLayout(page, expectedMode, phase) {
  const metrics = await waitFor(`${phase} log layout`, async () => page.evaluate((mode) => {
    const control = document.querySelector(".control-card");
    const card = document.querySelector(".details-card");
    const host = document.querySelector("ha-ops-log");
    const log = host?.shadowRoot?.querySelector("pre");
    if (!control || !card || !log) {
      return null;
    }
    const controlRect = control.getBoundingClientRect();
    const cardRect = card.getBoundingClientRect();
    return {
      cardHeight: cardRect.height,
      controlHeight: controlRect.height,
      logOverflowY: getComputedStyle(log).overflowY,
      sameRow: Math.abs(controlRect.top - cardRect.top) < 2,
    };
  }).then((item) => {
    if (!item || item.logOverflowY !== "auto" && item.logOverflowY !== "scroll") {
      return null;
    }
    if (expectedMode === "matched") {
      return item.sameRow && Math.abs(item.cardHeight - item.controlHeight) <= 2 ? item : null;
    }
    return !item.sameRow && Math.abs(item.cardHeight - 500) <= 2 ? item : null;
  }), 5000);
  assert(metrics, `${phase} log layout nodes missing`);
  assert(metrics.logOverflowY === "auto" || metrics.logOverflowY === "scroll", `${phase} log overflow-y was ${metrics.logOverflowY}`);
  if (expectedMode === "matched") {
    assert(metrics.sameRow, `${phase} log and controls were not on the same row`);
    assert(Math.abs(metrics.cardHeight - metrics.controlHeight) <= 2, `${phase} log height ${metrics.cardHeight} did not match control height ${metrics.controlHeight}`);
  } else {
    assert(!metrics.sameRow, `${phase} expected stacked mobile layout`);
    assert(Math.abs(metrics.cardHeight - 500) <= 2, `${phase} mobile fallback log height was ${metrics.cardHeight}`);
  }
}

async function assertPreviewExpansionControls(page, phase) {
  const files = page.getByTestId("preview-file");
  const count = await files.count();
  assert(count > 0, `${phase} had no preview files`);
  const firstFile = files.first();
  const firstToggle = firstFile.getByRole("button", { name: "Expand Diff" });
  await firstToggle.click();
  await firstFile.getByRole("button", { name: "Collapse Diff" }).waitFor();
  await firstFile.getByRole("button", { name: "Collapse Diff" }).click();
  await firstFile.getByRole("button", { name: "Expand Diff" }).waitFor();

  await page.getByRole("button", { name: "Expand All" }).last().click();
  const expandedAfterAll = await files.evaluateAll((items) =>
    items.every((item) => item.expanded === true),
  );
  assert(expandedAfterAll, `${phase} Expand All did not show all details`);

  await page.getByRole("button", { name: "Collapse All" }).last().click();
  const collapsedAfterAll = await files.evaluateAll((items) =>
    items.every((item) => item.expanded === false),
  );
  assert(collapsedAfterAll, `${phase} Collapse All did not hide all details`);
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function submitDuplicateFromBrowser(page, buttonName) {
  return page.evaluate((name) => {
    const buttons = Array.from(document.querySelectorAll("vaadin-button"));
    const button = buttons.find((candidate) => (candidate.textContent || "").trim() === name);
    const form = button?.closest("form");
    if (!form) {
      throw new Error(`form not found for ${name}`);
    }
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    return true;
  }, buttonName);
}

async function runPreviewScenario(page, baseUrl, action, buttonName, expectedText) {
  await assertStatusNot(page, "running", `${action} before preview`);
  await assertPreviewButtonsState(page, false, `${action} before preview`);
  const beforeControls = await stateChangingControls(page);
  await harnessPost(baseUrl, "__dev_harness__/arm", { action, gate: "running" });
  const loadId = await page.evaluate(() => window.__haOpsLoadId);
  await page.getByRole("button", { name: buttonName }).click();
  const heldState = await waitFor(`${action} held`, async () => {
    const state = await diagnostics(baseUrl);
    return state.gates[`${action}:running`]?.held ? state : null;
  });
  assert((heldState.counters.started_jobs[action] || 0) === 1, `${action} did not start exactly one gated job`);
  await page.locator("[data-status-code='running']").waitFor({ timeout: 5000 });
  await assertPreviewButtonsState(page, true, `${action} during preview`);
  await assertAllStateChangingControlsDisabled(page, `${action} during preview`);
  const disabledStyle = await page.getByRole("button", { name: buttonName }).evaluate((button) => {
    const style = window.getComputedStyle(button);
    return { color: style.color, background: style.backgroundColor, border: style.borderColor };
  });
  assert(disabledStyle.background !== "rgba(0, 0, 0, 0)", "disabled button has no visible background");
  await assertLogContains(page, "running", `${action} running`);
  const beforeDuplicate = await runningDomSnapshot(page, buttonName);

  await submitDuplicateFromBrowser(page, buttonName);
  await waitFor(`${action} duplicate rejected`, async () => {
    const state = await diagnostics(baseUrl);
    return (state.counters.duplicate_rejections[action] || 0) === 1 ? state : null;
  });
  const afterDuplicate = await runningDomSnapshot(page, buttonName);
  assert((afterDuplicate.loadId) === loadId, "page reloaded during duplicate rejection");
  assert((afterDuplicate.status) === "running", `${action} duplicate rejection reset running status`);
  assert(afterDuplicate.buttonDisabled, `${buttonName} was re-enabled while ${action} was still running`);
  assert(
    afterDuplicate.buttonText === beforeDuplicate.buttonText,
    `${buttonName} text changed during duplicate rejection: ${beforeDuplicate.buttonText} -> ${afterDuplicate.buttonText}`,
  );
  assert(
    afterDuplicate.details === beforeDuplicate.details,
    `${action} running details changed during duplicate rejection`,
  );
  const duplicateState = await diagnostics(baseUrl);
  assert((duplicateState.counters.started_jobs[action] || 0) === 1, `${action} duplicate started another job`);

  await harnessPost(baseUrl, "__dev_harness__/release", { action, gate: "running" });
  await page.getByTestId("operation-log").getByText(expectedText).waitFor({ timeout: 5000 });
  assert((await page.evaluate(() => window.__haOpsLoadId)) === loadId, "page reloaded during WebSocket update");
  await assertStatus(page, "success", `${action} after preview`);
  await assertPreviewButtonsState(page, false, `${action} after preview`);
  await assertPreviouslyEnabledControlsRestored(page, beforeControls, `${action} after preview`);
  await assertLogContains(page, expectedText, `${action} success`);
  await page.getByText("homeassistant/configuration.yaml").waitFor({ timeout: 5000 });
  await assertLogPanelLayout(page, "matched", `${action} after preview`);
  await assertPreviewExpansionControls(page, `${action} after reactive state update`);
}

async function main() {
  const { child, ready } = startHarness();
  let browser;
  try {
    const info = await ready;
    const baseUrl = info.baseUrl;
    browser = await chromium.launch({ headless: true });

    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    await context.addInitScript(() => {
      window.__HA_OPS_ENABLE_TEST_HOOKS__ = true;
      window.__haOpsLoadId = `${Date.now()}-${Math.random()}`;
    });
    const page = await context.newPage();
    await page.goto(baseUrl);
    await assertLogPanelLayout(page, "matched", "initial desktop");

    await runPreviewScenario(page, baseUrl, "preview", "Preview Git to HA", "Harness Git to HA preview finished.");
    await waitFor("first WebSocket replay", async () => {
      const state = await diagnostics(baseUrl);
      return state.counters.ws_replays_seen > 0 ? state : null;
    });
    const applyCursor = await page.evaluate(async () => {
      const snapshot = await fetch("debug-snapshot").then((response) => response.json());
      return snapshot.state.last_diff_cursor;
    });
    const applyDiff = await page.evaluate(async (cursor) => {
      return fetch(`diff-get?cursor=${encodeURIComponent(JSON.stringify(cursor))}`).then((response) => response.json());
    }, applyCursor);
    assert(applyDiff.ok && applyDiff.diff.includes("harness_git_only"), "apply diff_get did not return harness diff");

    await runPreviewScenario(page, baseUrl, "save_preview", "Preview HA to Git", "Harness HA to Git preview finished.");
    const saveCursor = await page.evaluate(async () => {
      const snapshot = await fetch("debug-snapshot").then((response) => response.json());
      return snapshot.state.last_save_diff_cursor;
    });
    const saveDiff = await page.evaluate(async (cursor) => {
      return fetch(`diff-get?cursor=${encodeURIComponent(JSON.stringify(cursor))}`).then((response) => response.json());
    }, saveCursor);
    assert(saveDiff.ok && saveDiff.diff.includes("harness_live_only"), "save diff_get did not return harness diff");

    await harnessPost(baseUrl, "__dev_harness__/arm", { action: "preview", gate: "running" });
    await page.getByRole("button", { name: "Preview Git to HA" }).click();
    await waitFor("preview held before reconnect", async () => {
      const state = await diagnostics(baseUrl);
      return state.gates["preview:running"]?.held ? state : null;
    });
    const beforeReplay = (await diagnostics(baseUrl)).counters.ws_replays_seen;
    await page.evaluate(() => window.__haOpsTestCloseWs());
    await waitFor("WebSocket reconnect replay", async () => {
      const state = await diagnostics(baseUrl);
      return state.counters.ws_replays_seen > beforeReplay ? state : null;
    }, 7000);
    assert((await page.locator("[data-status-code]").getAttribute("data-status-code")) === "running", "reconnect did not replay running state");
    await harnessPost(baseUrl, "__dev_harness__/release", { action: "preview", gate: "running" });
    await page.getByText("Harness Git to HA preview finished.").waitFor({ timeout: 5000 });

    const fallbackContext = await browser.newContext();
    const fallbackPosts = [];
    fallbackContext.on("request", (request) => {
      if (request.method() === "POST") {
        fallbackPosts.push(new URL(request.url()).pathname);
      }
    });
    await fallbackContext.addInitScript(() => {
      Object.defineProperty(window, "WebSocket", { value: undefined, configurable: true });
      Object.defineProperty(window.crypto, "randomUUID", { value: undefined, configurable: true });
      window.__haOpsLoadId = `${Date.now()}-${Math.random()}`;
    });
    const fallbackPage = await fallbackContext.newPage();
    const fallbackErrors = [];
    fallbackPage.on("pageerror", (error) => fallbackErrors.push(error.message));
    await fallbackPage.goto(baseUrl);
    await fallbackPage.getByTestId("connection-status").getByText("http").waitFor({ timeout: 5000 });
    const beforeFallback = await diagnostics(baseUrl);
    const beforePreviewComplete = beforeFallback.counters.completed_jobs.preview || 0;
    const beforeSavePreviewComplete = beforeFallback.counters.completed_jobs.save_preview || 0;
    const previewFallbackResponse = fallbackPage.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/preview"), { timeout: 5000 });
    await fallbackPage.getByRole("button", { name: "Preview Git to HA" }).click();
    const previewFallbackPayload = await (await previewFallbackResponse.catch(async (error) => {
      throw new Error(`${error.message}; pageErrors=${fallbackErrors.join(" | ")}; clientStatus=${await fallbackPage.locator("#client-status").textContent()}`);
    })).json();
    assert(previewFallbackPayload.ok, `fetch fallback Preview Git to HA rejected: ${JSON.stringify(previewFallbackPayload)}`);
    await waitFor("fetch fallback Preview Git to HA completion", async () => {
      const state = await diagnostics(baseUrl);
      return (state.counters.completed_jobs.preview || 0) > beforePreviewComplete ? state : null;
    });
    const saveFallbackResponse = fallbackPage.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/save-preview"));
    await fallbackPage.getByRole("button", { name: "Preview HA to Git" }).click();
    const saveFallbackPayload = await (await saveFallbackResponse).json();
    assert(saveFallbackPayload.ok, `fetch fallback Preview HA to Git rejected: ${JSON.stringify(saveFallbackPayload)}`);
    await waitFor("fetch fallback Preview HA to Git completion", async () => {
      const state = await diagnostics(baseUrl);
      return (state.counters.completed_jobs.save_preview || 0) > beforeSavePreviewComplete ? state : null;
    });
    assert(
      fallbackPosts.some((item) => item.endsWith("/api/hassio_ingress/local-ha-ops/preview")),
      `fetch fallback did not POST ingress Preview Git to HA; posts=${fallbackPosts.join(",")}`,
    );
    assert(
      fallbackPosts.some((item) => item.endsWith("/api/hassio_ingress/local-ha-ops/save-preview")),
      `fetch fallback did not POST ingress Preview HA to Git; posts=${fallbackPosts.join(",")}`,
    );
    await fallbackContext.close();

    const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const mobilePage = await mobileContext.newPage();
    await mobilePage.goto(baseUrl);
    await assertLogPanelLayout(mobilePage, "fallback", "initial mobile");
    await mobileContext.close();

    const debugText = await page.evaluate(() => fetch("debug-snapshot").then((response) => response.text()));
    assert(!debugText.includes("diff --git"), "debug snapshot leaked raw diff");
    console.log(`browser smoke passed: ${baseUrl}`);
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    let cleanupError = null;
    if (browser) {
      try {
        await browser.close();
      } catch (error) {
        cleanupError = error;
      }
    }
    try {
      await stopHarness(child);
    } catch (error) {
      cleanupError = cleanupError || error;
    }
    if (cleanupError) {
      if (primaryError) {
        console.error(`cleanup after failure also failed: ${cleanupError.stack || cleanupError.message}`);
      } else {
        throw cleanupError;
      }
    }
  }
}

let primaryError = null;
try {
  await main();
} catch (error) {
  console.error(error.stack || error.message);
  process.exitCode = 1;
}
