import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, "../..");
const repoRoot = path.resolve(appRoot, "..");
const sharedRoot = process.env.PLAYWRIGHT_SHARED_ROOT || "/Users/purportex/Applications/Playwright";
const runtime = await import(pathToFileURL(path.join(sharedRoot, "src/runtime.mjs")).href);
const { chromium } = runtime;
const PREVIEW_BUTTONS = ["Preview Git to HA", "Preview HA to Git"];

function sortedStrings(items) {
  return [...(items || [])].map((item) => String(item)).filter(Boolean).sort();
}

function sortedObject(value) {
  return Object.fromEntries(Object.entries(value || {}).sort(([left], [right]) => left.localeCompare(right)));
}

function cursorIdentity(cursor) {
  if (!cursor || typeof cursor !== "object") return null;
  return Object.fromEntries(
    ["schema", "kind", "generation", "artifact", "sha256", "bytes"]
      .filter((key) => Object.hasOwn(cursor, key))
      .map((key) => [key, cursor[key]]),
  );
}

function previewIdentity(state, direction) {
  if (direction === "save") {
    return {
      direction: "save",
      commit: state.last_save_preview_commit ?? null,
      fingerprint: state.last_save_preview_fingerprint ?? null,
      paths: sortedStrings(state.last_save_preview_paths),
      conflict_paths: sortedStrings(state.last_save_preview_conflict_paths),
      diff_cursor: cursorIdentity(state.last_save_diff_cursor),
    };
  }
  return {
    direction: "apply",
    commit: state.last_preview_commit ?? null,
    fingerprint: state.last_preview_fingerprint ?? null,
    live_fingerprints: sortedObject(state.last_preview_live_fingerprints),
    paths: sortedStrings(state.last_preview_paths),
    conflict_paths: sortedStrings(state.last_preview_conflict_paths),
    diff_cursor: cursorIdentity(state.last_diff_cursor),
  };
}

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
        return Array.from(form.querySelectorAll("vaadin-button, vaadin-checkbox, vaadin-details, vaadin-select, input:not([type='hidden']), textarea"))
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

async function waitForInteractiveTransport(page, phase) {
  await waitFor(`${phase} interactive transport`, async () => {
    const connection = await page.getByTestId("status-badge").getAttribute("data-connection-state");
    return ["connected", "http"].includes(connection) ? { connection } : null;
  }, 7000);
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
  await firstFile.locator("vaadin-details-summary").click();
  await waitFor(`${phase} first details opened`, async () => {
    return await firstFile.evaluate((item) => item.expanded === true) ? true : null;
  });
  await firstFile.locator("vaadin-details-summary").click();
  await waitFor(`${phase} first details closed`, async () => {
    return await firstFile.evaluate((item) => item.expanded === false) ? true : null;
  });

  if (count > 1) {
    const secondFile = files.nth(1);
    await firstFile.locator("vaadin-details-summary").click();
    await secondFile.locator("vaadin-details-summary").click();
    const firstTwoExpanded = await files.evaluateAll((items) =>
      items.slice(0, 2).every((item) => item.expanded === true),
    );
    assert(firstTwoExpanded, `${phase} opening a second Details row closed the first row`);
  }

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

async function assertDiffSectionMountedBeforeGitAccess(page, phase, expectedLoading) {
  await page.getByTestId("reactive-previews").waitFor({ timeout: 5000 });
  const counts = await page.evaluate(() => ({
    hosts: document.querySelectorAll('[data-testid="reactive-previews"]').length,
    cards: document.querySelectorAll('section.card.wide[data-testid="diff-section"]').length,
  }));
  assert(counts.hosts === 1, `${phase} reactive preview host count was ${counts.hosts}`);
  assert(counts.cards === 1, `${phase} diff section count was ${counts.cards}`);
  const placement = await page.evaluate(() => {
    const diff = document.querySelector('section.card.wide[data-testid="diff-section"]');
    const gitAccess = Array.from(document.querySelectorAll("section.card.wide"))
      .find((section) => section.querySelector("h2")?.textContent?.trim() === "Git Access");
    return {
      diffTop: diff?.getBoundingClientRect().top ?? null,
      gitTop: gitAccess?.getBoundingClientRect().top ?? null,
      loading: diff?.textContent?.includes("Loading Diff...") ?? false,
    };
  });
  assert(placement.diffTop !== null && placement.gitTop !== null, `${phase} missing diff or Git Access section`);
  assert(placement.diffTop < placement.gitTop, `${phase} diff section was not before Git Access`);
  assert(placement.loading === expectedLoading, `${phase} loading state was ${placement.loading}, expected ${expectedLoading}`);
}

async function assertPreviewRowsAndControls(page, phase) {
  await assertDiffSectionMountedBeforeGitAccess(page, phase, false);
  const files = page.getByTestId("preview-file");
  const count = await files.count();
  assert(count > 0, `${phase} had no preview files`);
  const controls = await files.evaluateAll((items) => items.map((item) => {
    const root = item.shadowRoot;
    return {
      checkbox: Boolean(root?.querySelector("vaadin-checkbox")),
      details: Boolean(root?.querySelector("vaadin-details")),
      radio: Boolean(root?.querySelector("vaadin-radio-group")),
      wrapButton: Boolean(Array.from(root?.querySelectorAll("vaadin-button") || []).find((button) => button.textContent.trim() === "Wrap Lines")),
      haButton: Boolean(Array.from(root?.querySelectorAll("vaadin-button") || []).find((button) => button.textContent.trim() === "Use HA Version")),
      gitButton: Boolean(Array.from(root?.querySelectorAll("vaadin-button") || []).find((button) => button.textContent.trim() === "Use Git Version")),
      path: root?.querySelector("code")?.textContent || "",
    };
  }));
  const missing = controls.filter((item) => !item.checkbox || !item.details || !item.wrapButton || !item.haButton || !item.gitButton || !item.path || item.radio);
  assert(missing.length === 0, `${phase} preview rows missing checkbox/details/buttons/path or still had radio group: ${JSON.stringify(controls)}`);
}

async function assertWrapControlsAndOverflow(page, baseUrl, diffGetRequests, phase) {
  await waitFor(`${phase} save preview mounted`, async () => page.evaluate(() => {
    return Array.from(document.querySelectorAll("ha-ops-preview")).some((item) => item.direction === "save") ? true : null;
  }));
  const order = await page.evaluate(() => {
    const preview = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "save");
    const previewRoot = preview?.shadowRoot;
    const toolbarLabels = Array.from(previewRoot?.querySelectorAll("header vaadin-button") || []).map((button) => button.textContent.trim());
    const firstFile = previewRoot?.querySelector("ha-ops-preview-file");
    const rowLabels = Array.from(firstFile?.shadowRoot?.querySelectorAll(".choice vaadin-button") || []).map((button) => button.textContent.trim());
    return {
      toolbarLabels,
      rowLabels,
      globalWrapIndex: toolbarLabels.indexOf("Wrap All Lines"),
      selectAllIndex: toolbarLabels.indexOf("Select All"),
      rowWrapIndex: rowLabels.indexOf("Wrap Lines"),
      haIndex: rowLabels.indexOf("Use HA Version"),
      gitIndex: rowLabels.indexOf("Use Git Version"),
    };
  });
  assert(order.globalWrapIndex >= 0 && order.globalWrapIndex < order.selectAllIndex, `${phase} global wrap was not before Select All: ${JSON.stringify(order.toolbarLabels)}`);
  assert(order.rowWrapIndex >= 0 && order.rowWrapIndex < order.haIndex && order.rowWrapIndex < order.gitIndex, `${phase} row wrap was not before version buttons: ${JSON.stringify(order.rowLabels)}`);

  await page.getByRole("button", { name: "Expand All" }).last().click();
  await page.getByText("harness_long_line").waitFor({ timeout: 5000 });
  const beforeRequests = diffGetRequests.length;
  const unwrappedMetrics = await page.evaluate(() => {
    const doc = document.documentElement;
    const preview = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "save");
    const firstFile = preview?.shadowRoot?.querySelector("ha-ops-preview-file");
    const pre = firstFile?.shadowRoot?.querySelector("pre");
    const fileRect = firstFile?.getBoundingClientRect();
    const style = pre ? getComputedStyle(pre) : null;
    return {
      docClientWidth: doc.clientWidth,
      docScrollWidth: doc.scrollWidth,
      fileRight: fileRect?.right ?? null,
      preClientWidth: pre?.clientWidth ?? 0,
      preScrollWidth: pre?.scrollWidth ?? 0,
      overflowX: style?.overflowX ?? "",
      whiteSpace: style?.whiteSpace ?? "",
    };
  });
  assert(unwrappedMetrics.docScrollWidth <= unwrappedMetrics.docClientWidth + 2, `${phase} page overflowed before wrap: ${JSON.stringify(unwrappedMetrics)}`);
  assert(unwrappedMetrics.fileRight <= unwrappedMetrics.docClientWidth + 2, `${phase} preview row overflowed viewport: ${JSON.stringify(unwrappedMetrics)}`);
  assert(unwrappedMetrics.preScrollWidth > unwrappedMetrics.preClientWidth, `${phase} unwrapped diff did not scroll internally: ${JSON.stringify(unwrappedMetrics)}`);
  assert(["auto", "scroll"].includes(unwrappedMetrics.overflowX), `${phase} unwrapped diff overflow-x was ${unwrappedMetrics.overflowX}`);
  assert(unwrappedMetrics.whiteSpace === "pre", `${phase} unwrapped diff white-space was ${unwrappedMetrics.whiteSpace}`);

  const afterRowWrap = await page.evaluate(async () => {
    const preview = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "save");
    const files = Array.from(preview?.shadowRoot?.querySelectorAll("ha-ops-preview-file") || []);
    const first = files[0];
    const second = files[1];
    const button = Array.from(first.shadowRoot.querySelectorAll("vaadin-button")).find((item) => item.textContent.trim() === "Wrap Lines");
    button.click();
    await preview.updateComplete;
    await first.updateComplete;
    await second.updateComplete;
    const firstPre = first.shadowRoot.querySelector("pre");
    const secondPre = second.shadowRoot.querySelector("pre");
    return {
      firstWrapped: first.wrapLines,
      secondWrapped: second.wrapLines,
      firstWhiteSpace: getComputedStyle(firstPre).whiteSpace,
      secondWhiteSpace: getComputedStyle(secondPre).whiteSpace,
      firstClientWidth: firstPre.clientWidth,
      firstScrollWidth: firstPre.scrollWidth,
    };
  });
  assert(afterRowWrap.firstWrapped && !afterRowWrap.secondWrapped, `${phase} per-file wrap affected wrong rows: ${JSON.stringify(afterRowWrap)}`);
  assert(afterRowWrap.firstWhiteSpace === "pre-wrap" && afterRowWrap.secondWhiteSpace === "pre", `${phase} per-file wrap styles were wrong: ${JSON.stringify(afterRowWrap)}`);
  assert(afterRowWrap.firstScrollWidth <= afterRowWrap.firstClientWidth + 4, `${phase} wrapped diff still overflowed internally: ${JSON.stringify(afterRowWrap)}`);
  assert(diffGetRequests.length === beforeRequests, `${phase} wrap toggle triggered diff-get`);

  const refreshSnapshot = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
  const afterSamePreviewRefresh = await postPreviewDecision(baseUrl, "select_save_preview", refreshSnapshot.state.operation_generation, {
    path: refreshSnapshot.state.last_save_preview_paths[0],
    selected: "1",
    preview_identity: previewIdentity(refreshSnapshot.state, "save"),
  });
  assert(afterSamePreviewRefresh.ok, `${phase} same-preview select failed before wrap preservation check: ${JSON.stringify(afterSamePreviewRefresh)}`);
  await waitFor(`${phase} same-preview wrap preservation`, async () => {
    const wrapped = await page.evaluate(() => {
      const preview = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "save");
      return preview?.shadowRoot?.querySelector("ha-ops-preview-file")?.wrapLines;
    });
    return wrapped ? { wrapped } : null;
  });

  const global = await page.evaluate(async () => {
    const preview = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "save");
    const globalButton = Array.from(preview.shadowRoot.querySelectorAll("header vaadin-button")).find((button) => button.textContent.trim() === "Wrap All Lines");
    globalButton.click();
    await preview.updateComplete;
    const wrapped = Array.from(preview.shadowRoot.querySelectorAll("ha-ops-preview-file")).map((file) => file.wrapLines);
    const unwrapButton = Array.from(preview.shadowRoot.querySelectorAll("header vaadin-button")).find((button) => button.textContent.trim() === "Unwrap All Lines");
    unwrapButton.click();
    await preview.updateComplete;
    const unwrapped = Array.from(preview.shadowRoot.querySelectorAll("ha-ops-preview-file")).map((file) => file.wrapLines);
    return { wrapped, unwrapped };
  });
  assert(global.wrapped.every(Boolean), `${phase} Wrap All did not wrap all rows: ${JSON.stringify(global)}`);
  assert(global.unwrapped.every((value) => !value), `${phase} Unwrap All did not unwrap all rows: ${JSON.stringify(global)}`);
  assert(diffGetRequests.length === beforeRequests, `${phase} global wrap toggle triggered diff-get`);
}

async function assertSaveCommitSubjectFlow(page, baseUrl) {
  await waitFor("save commit subject preview mounted", async () => page.evaluate(() => {
    return Array.from(document.querySelectorAll("ha-ops-preview")).some((item) => item.direction === "save") ? true : null;
  }));
  const applyInputCount = await page.evaluate(() => {
    const apply = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "apply");
    return apply?.shadowRoot?.querySelectorAll("input[name='commit_subject']").length || 0;
  });
  assert(applyInputCount === 0, `Apply preview exposed commit subject input: ${applyInputCount}`);

  const saveSubject = await page.evaluate(() => {
    const save = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "save");
    const root = save?.shadowRoot;
    const input = root?.querySelector("input[name='commit_subject']");
    const saveButton = Array.from(root?.querySelectorAll("footer vaadin-button") || []).find((button) => button.textContent.trim() === "Save HA to Git");
    return {
      value: input?.value ?? null,
      inputLeft: input?.getBoundingClientRect().left ?? null,
      buttonLeft: saveButton?.getBoundingClientRect().left ?? null,
    };
  });
  assert(saveSubject.value === "Harness save preview", `Save commit subject was not prefilled: ${JSON.stringify(saveSubject)}`);
  assert(saveSubject.inputLeft !== null && saveSubject.inputLeft < saveSubject.buttonLeft, `Save commit subject was not left of Save button: ${JSON.stringify(saveSubject)}`);

  await page.locator("ha-ops-preview").filter({ hasText: "Save HA to Git" }).locator("input[name='commit_subject']").fill("Browser Custom Save Subject");
  await page.getByRole("button", { name: "Select All" }).last().click();
  await waitFor("save subject flow selection", async () => {
    const snapshot = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    return snapshot.state.save_preview_selected_paths?.length ? snapshot : null;
  });

  await harnessPost(baseUrl, "__dev_harness__/arm", { action: "save", gate: "running" });
  await page.getByRole("button", { name: "Save HA to Git" }).click();
  await waitFor("save subject flow held", async () => {
    const state = await diagnostics(baseUrl);
    return state.gates["save:running"]?.held ? state : null;
  });
  const disabled = await page.evaluate(() => {
    const save = Array.from(document.querySelectorAll("ha-ops-preview")).find((item) => item.direction === "save");
    const root = save?.shadowRoot;
    const input = root?.querySelector("input[name='commit_subject']");
    const button = Array.from(root?.querySelectorAll("footer vaadin-button") || []).find((item) => item.textContent.trim() === "Save HA to Git");
    return { input: Boolean(input?.disabled), button: Boolean(button?.disabled) };
  });
  assert(disabled.input && disabled.button, `Save running state did not disable subject input and final button: ${JSON.stringify(disabled)}`);
  await harnessPost(baseUrl, "__dev_harness__/release", { action: "save", gate: "running" });
  await page.getByText("Harness live HA changes committed to Git.").waitFor({ timeout: 5000 });
  await waitFor("save subject submitted to harness", async () => {
    const state = await diagnostics(baseUrl);
    return state.last_save_commit_subject === "Browser Custom Save Subject" ? state : null;
  });
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

async function runPreviewScenario(page, baseUrl, action, buttonName, expectedText, diffGetRequests) {
  await assertStatusNot(page, "running", `${action} before preview`);
  await assertPreviewButtonsState(page, false, `${action} before preview`);
  const beforeControls = await stateChangingControls(page);
  const diffRequestsBefore = diffGetRequests.length;
  await harnessPost(baseUrl, "__dev_harness__/arm", { action, gate: "running" });
  const loadId = await page.evaluate(() => window.__haOpsLoadId);
  await page.getByRole("button", { name: buttonName }).click();
  const heldState = await waitFor(`${action} held`, async () => {
    const state = await diagnostics(baseUrl);
    return state.gates[`${action}:running`]?.held ? state : null;
  });
  assert((heldState.counters.started_jobs[action] || 0) === 1, `${action} did not start exactly one gated job`);
  await page.locator("[data-status-code='running']").waitFor({ timeout: 5000 });
  await assertDiffSectionMountedBeforeGitAccess(page, `${action} running`, true);
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
  await assertPreviewRowsAndControls(page, `${action} after preview`);
  assert(diffGetRequests.length === diffRequestsBefore, `${action} fetched diff before explicit expansion`);
  if (action === "preview") {
    await assertDetailsControlEventIsolation(page, baseUrl, "apply", diffGetRequests, `${action} after preview`);
    assert(diffGetRequests.length === diffRequestsBefore, `${action} row controls fetched diff before explicit expansion`);
  }
  await assertLogPanelLayout(page, "matched", `${action} after preview`);
  await assertPreviewExpansionControls(page, `${action} after reactive state update`);
  assert(diffGetRequests.length > diffRequestsBefore, `${action} did not fetch diff after explicit expansion`);
}

async function postPreviewDecision(baseUrl, command, generation, payload) {
  const response = await fetch(`${baseUrl}${command.replaceAll("_", "-")}`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify({
      command_id: randomUUID(),
      command,
      generation,
      payload,
    }),
  });
  return response.json();
}

async function assertSamePreviewDecisionRefreshKeepsDiff(page, baseUrl, direction) {
  const snapshot = await page.evaluate(() => fetch("debug-snapshot").then((response) => response.json()));
  const state = snapshot.state;
  const identity = previewIdentity(state, direction);
  const paths = direction === "save" ? state.last_save_preview_paths : state.last_preview_paths;
  const cursor = direction === "save" ? state.last_save_diff_cursor : state.last_diff_cursor;
  assert(paths.length >= 2, `${direction} preview did not expose two browser decision paths`);

  await page.getByRole("button", { name: "Expand All" }).last().click();
  await page.getByText(direction === "save" ? "harness_live_package" : "harness_git_package").waitFor({ timeout: 5000 });

  const selectCommand = direction === "save" ? "select_save_preview" : "select_apply_preview";
  const resolveCommand = direction === "save" ? "resolve_save_preview" : "resolve_apply_preview";
  const first = await postPreviewDecision(baseUrl, selectCommand, state.operation_generation, {
    path: paths[0],
    selected: "1",
    preview_identity: identity,
  });
  assert(first.ok, `${direction} same-preview select rejected: ${JSON.stringify(first)}`);
  await waitFor(`${direction} first decision refresh`, async () => {
    const current = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    const selected = direction === "save"
      ? current.state.save_preview_selected_paths
      : current.state.apply_preview_selected_paths;
    return selected?.includes(paths[0]) ? current : null;
  });

  const second = await postPreviewDecision(baseUrl, resolveCommand, state.operation_generation, {
    path: paths[1],
    choice: direction === "save" ? "git" : "ha",
    preview_identity: identity,
  });
  assert(second.ok, `${direction} old same-preview resolve rejected after refresh: ${JSON.stringify(second)}`);

  const diff = await page.evaluate(async (savedCursor) => {
    return fetch(`diff-get?cursor=${encodeURIComponent(JSON.stringify(savedCursor))}`).then((response) => response.json());
  }, cursor);
  assert(diff.ok, `${direction} diff cursor failed after same-preview decisions: ${JSON.stringify(diff)}`);
  const expanded = await page.getByTestId("preview-file").evaluateAll((items) => items.every((item) => item.expanded === true));
  assert(expanded, `${direction} expanded diff state was lost after decision refresh`);

  const oldIdentity = identity;
  await page.getByRole("button", { name: direction === "save" ? "Preview HA to Git" : "Preview Git to HA" }).click();
  await page.getByText(direction === "save" ? "Harness HA to Git preview finished." : "Harness Git to HA preview finished.").waitFor({ timeout: 5000 });
  const afterReplace = await page.evaluate(() => fetch("debug-snapshot").then((response) => response.json()));
  const stale = await postPreviewDecision(baseUrl, selectCommand, afterReplace.state.operation_generation, {
    path: paths[0],
    selected: "1",
    preview_identity: oldIdentity,
  });
  assert(!stale.ok, `${direction} stale select mutated replaced preview: ${JSON.stringify(stale)}`);
  const afterStale = await page.evaluate(() => fetch("debug-snapshot").then((response) => response.json()));
  const selectedAfterStale = direction === "save"
    ? afterStale.state.save_preview_selected_paths
    : afterStale.state.apply_preview_selected_paths;
  assert(selectedAfterStale.length === 0, `${direction} stale select repopulated replaced preview`);
}

async function assertDetailsControlEventIsolation(page, baseUrl, direction, diffGetRequests, phase) {
  const snapshot = await page.evaluate(() => fetch("debug-snapshot").then((response) => response.json()));
  const state = snapshot.state;
  const paths = direction === "save" ? state.last_save_preview_paths : state.last_preview_paths;
  const path = paths?.[0];
  assert(path, `${phase} had no preview path for event isolation`);
  const file = page.getByTestId("preview-file").filter({ hasText: path }).first();
  const checkbox = file.locator("vaadin-checkbox").first();
  const haButton = file.getByRole("button", { name: "Use HA Version" });
  const gitButton = file.getByRole("button", { name: "Use Git Version" });
  const beforeRequests = diffGetRequests.length;

  await checkbox.click();
  await waitFor(`${phase} row selected`, async () => {
    const current = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    const selected = direction === "save"
      ? current.state.save_preview_selected_paths
      : current.state.apply_preview_selected_paths;
    return selected?.includes(path) ? current : null;
  });
  assert(await file.evaluate((item) => item.expanded === false), `${phase} checkbox click toggled Details`);
  assert(diffGetRequests.length === beforeRequests, `${phase} checkbox click fetched a diff`);

  await haButton.click();
  await waitFor(`${phase} HA choice persisted`, async () => {
    const current = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    const resolutions = direction === "save"
      ? current.state.save_preview_resolutions
      : current.state.apply_preview_resolutions;
    return resolutions?.[path] === "ha" ? current : null;
  });
  assert(await file.evaluate((item) => item.expanded === false), `${phase} HA button click toggled Details`);
  assert(diffGetRequests.length === beforeRequests, `${phase} HA button click fetched a diff`);

  await gitButton.focus();
  await page.keyboard.press("Enter");
  await waitFor(`${phase} Git choice persisted`, async () => {
    const current = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    const resolutions = direction === "save"
      ? current.state.save_preview_resolutions
      : current.state.apply_preview_resolutions;
    return resolutions?.[path] === "git" ? current : null;
  });
  assert(await file.evaluate((item) => item.expanded === false), `${phase} Git keyboard activation toggled Details`);
  assert(diffGetRequests.length === beforeRequests, `${phase} Git keyboard activation fetched a diff`);

  await haButton.focus();
  await page.keyboard.press(" ");
  await waitFor(`${phase} HA keyboard choice persisted`, async () => {
    const current = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    const resolutions = direction === "save"
      ? current.state.save_preview_resolutions
      : current.state.apply_preview_resolutions;
    return resolutions?.[path] === "ha" ? current : null;
  });
  assert(await file.evaluate((item) => item.expanded === false), `${phase} HA Space activation toggled Details`);
  assert(diffGetRequests.length === beforeRequests, `${phase} HA Space activation fetched a diff`);
}

async function assertSaveNormalChoiceControls(page, baseUrl) {
  const snapshot = await page.evaluate(() => fetch("debug-snapshot").then((response) => response.json()));
  const state = snapshot.state;
  const path = state.last_save_preview_paths?.[0];
  assert(path, "save preview has no path for choice controls");
  const file = page.getByTestId("preview-file").filter({ hasText: path }).first();
  const checkbox = file.locator("vaadin-checkbox").first();
  const alreadySelected = await checkbox.evaluate((control) => control.checked);
  if (!alreadySelected) await checkbox.click();
  await waitFor("save preview selected path", async () => {
    const current = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    return current.state.save_preview_selected_paths?.includes(path) ? current : null;
  });
  const valueAfterSelect = await file.getByRole("button", { name: "Use HA Version" }).evaluate((control) => control.getAttribute("aria-pressed"));
  assert(valueAfterSelect === "true", `normal Save row HA default active state was ${valueAfterSelect}`);
  await file.getByText("Use Git Version").click();
  await waitFor("save preview Git override", async () => {
    const current = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    return current.state.save_preview_resolutions?.[path] === "git" ? current : null;
  });
}

async function assertMobilePreviewUsability(page, phase) {
  const metrics = await page.evaluate(() => {
    const doc = document.documentElement;
    function allElements(root = document) {
      const elements = Array.from(root.querySelectorAll("*"));
      return elements.flatMap((element) => [element, ...(element.shadowRoot ? allElements(element.shadowRoot) : [])]);
    }
    const offenders = Array.from(document.querySelectorAll("body *")).map((element) => {
      const rect = element.getBoundingClientRect();
      return {
        tag: element.tagName.toLowerCase(),
        testid: element.getAttribute("data-testid") || "",
        className: typeof element.className === "string" ? element.className : "",
        text: (element.textContent || "").trim().slice(0, 80),
        left: rect.left,
        right: rect.right,
        width: rect.width,
      };
    }).filter((item) => item.right > doc.clientWidth + 2 || item.left < -2)
      .sort((left, right) => right.right - left.right)
      .slice(0, 8);
    const controls = allElements()
      .filter((element) => ["VAADIN-CHECKBOX", "VAADIN-DETAILS", "VAADIN-BUTTON", "INPUT"].includes(element.tagName))
      .filter((element) =>
        element.closest("ha-ops-preview-file")
        || element.getRootNode()?.host?.closest?.("ha-ops-preview-file")
        || element.getAttribute("name") === "commit_subject",
      )
      .map((control) => {
        const rect = control.getBoundingClientRect();
        return { width: rect.width, height: rect.height, left: rect.left, right: rect.right };
      });
    return {
      clientWidth: doc.clientWidth,
      scrollWidth: doc.scrollWidth,
      controls,
      offenders,
    };
  });
  assert(metrics.scrollWidth <= metrics.clientWidth + 2, `${phase} page overflowed horizontally: ${metrics.scrollWidth} > ${metrics.clientWidth}; offenders=${JSON.stringify(metrics.offenders)}`);
  assert(metrics.controls.length > 0, `${phase} had no mobile preview controls`);
  const hidden = metrics.controls.filter((rect) => rect.width <= 0 || rect.height <= 0 || rect.right < 0 || rect.left > metrics.clientWidth);
  assert(hidden.length === 0, `${phase} had invisible/offscreen preview controls: ${JSON.stringify(hidden)}`);
}

async function runHttpFallbackFlow(page, baseUrl, posts, flow) {
  const before = await diagnostics(baseUrl);
  const beforePreviewComplete = before.counters.completed_jobs[flow.previewAction] || 0;
  const beforeFinalComplete = before.counters.completed_jobs[flow.finalAction] || 0;

  const previewResponse = page.waitForResponse(
    (response) => response.request().method() === "POST" && response.url().endsWith(`/${flow.previewPath}`),
    { timeout: 5000 },
  );
  await page.getByRole("button", { name: flow.previewButton }).click();
  const previewPayload = await (await previewResponse.catch(async (error) => {
    throw new Error(`${error.message}; clientStatus=${await page.locator("#client-status").textContent()}`);
  })).json();
  assert(previewPayload.ok, `fetch fallback ${flow.previewButton} rejected: ${JSON.stringify(previewPayload)}`);
  await waitFor(`fetch fallback ${flow.previewButton} completion`, async () => {
    const state = await diagnostics(baseUrl);
    return (state.counters.completed_jobs[flow.previewAction] || 0) > beforePreviewComplete ? state : null;
  });
  await page.getByText("homeassistant/configuration.yaml").waitFor({ timeout: 5000 });
  await page.getByRole("button", { name: "Select All" }).last().click();
  await waitFor(`fetch fallback ${flow.previewButton} selection`, async () => {
    const snapshot = await fetch(`${baseUrl}debug-snapshot`).then((response) => response.json());
    const selected = flow.finalAction === "save"
      ? snapshot.state.save_preview_selected_paths
      : snapshot.state.apply_preview_selected_paths;
    return selected?.length ? snapshot : null;
  });

  const finalResponse = page.waitForResponse(
    (response) => response.request().method() === "POST" && response.url().endsWith(`/${flow.finalPath}`),
    { timeout: 5000 },
  );
  await page.getByRole("button", { name: flow.finalButton }).click();
  const finalPayload = await (await finalResponse).json();
  assert(finalPayload.ok, `fetch fallback ${flow.finalButton} rejected: ${JSON.stringify(finalPayload)}`);
  await waitFor(`fetch fallback ${flow.finalButton} completion`, async () => {
    const state = await diagnostics(baseUrl);
    return (state.counters.completed_jobs[flow.finalAction] || 0) > beforeFinalComplete ? state : null;
  });
  await page.getByTestId("operation-log").getByText(flow.finalText).waitFor({ timeout: 5000 });
  await assertStatus(page, "success", `fetch fallback ${flow.finalButton}`);
  assert(
    posts.some((item) => item.endsWith(`/api/hassio_ingress/local-ha-ops/${flow.previewPath}`)),
    `fetch fallback did not POST ingress ${flow.previewButton}; posts=${posts.join(",")}`,
  );
  assert(
    posts.some((item) => item.endsWith(`/api/hassio_ingress/local-ha-ops/${flow.finalPath}`)),
    `fetch fallback did not POST ingress ${flow.finalButton}; posts=${posts.join(",")}`,
  );
}

async function main() {
  const { child, ready } = startHarness();
  let browser;
  try {
    const info = await ready;
    const baseUrl = info.baseUrl;
    browser = await chromium.launch({ headless: true });

    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    const websockets = [];
    await context.addInitScript(() => {
      window.__HA_OPS_ENABLE_TEST_HOOKS__ = true;
      window.__haOpsLoadId = `${Date.now()}-${Math.random()}`;
    });
    const page = await context.newPage();
    page.on("websocket", (ws) => websockets.push(ws.url()));
    const diffGetRequests = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.endsWith("/diff-get")) diffGetRequests.push(request.url());
    });
    await page.goto(baseUrl);
    await assertLogPanelLayout(page, "matched", "initial desktop");
    await page.getByTestId("status-badge").waitFor();
    await page.getByTestId("version-badge").getByText(/\d+\.\d+\.\d+/).waitFor();
    assert((await page.getByTestId("connection-status").count()) === 0, "production connection badge should not float over the UI");
    await new Promise((resolve) => setTimeout(resolve, 2500));
    assert(websockets.length === 1, `idle page created repeated WebSockets: ${websockets.length}`);

    await harnessPost(baseUrl, "__dev_harness__/clear-previews");
    await page.reload();
    await waitForInteractiveTransport(page, "apply preview page reload");
    await runPreviewScenario(page, baseUrl, "preview", "Preview Git to HA", "Harness Git to HA preview finished.", diffGetRequests);
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
    await assertSamePreviewDecisionRefreshKeepsDiff(page, baseUrl, "apply");

    await harnessPost(baseUrl, "__dev_harness__/clear-previews");
    await page.reload();
    await waitForInteractiveTransport(page, "save preview page reload");
    await runPreviewScenario(page, baseUrl, "save_preview", "Preview HA to Git", "Harness HA to Git preview finished.", diffGetRequests);
    await assertSaveNormalChoiceControls(page, baseUrl);
    const saveCursor = await page.evaluate(async () => {
      const snapshot = await fetch("debug-snapshot").then((response) => response.json());
      return snapshot.state.last_save_diff_cursor;
    });
    const saveDiff = await page.evaluate(async (cursor) => {
      return fetch(`diff-get?cursor=${encodeURIComponent(JSON.stringify(cursor))}`).then((response) => response.json());
    }, saveCursor);
    assert(saveDiff.ok && saveDiff.diff.includes("harness_live_only"), "save diff_get did not return harness diff");
    await assertSamePreviewDecisionRefreshKeepsDiff(page, baseUrl, "save");
    await assertWrapControlsAndOverflow(page, baseUrl, diffGetRequests, "save preview wrapping");
    await assertSaveCommitSubjectFlow(page, baseUrl);

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
    await waitFor("HTTP fallback status badge transport state", async () => {
      return (await fallbackPage.getByTestId("status-badge").getAttribute("data-connection-state")) === "http";
    });
    await runHttpFallbackFlow(fallbackPage, baseUrl, fallbackPosts, {
      previewAction: "preview",
      previewPath: "preview",
      previewButton: "Preview Git to HA",
      finalAction: "apply",
      finalPath: "apply",
      finalButton: "Apply Git to HA",
      finalText: "Harness Git automation applied to HA.",
    });
    await runHttpFallbackFlow(fallbackPage, baseUrl, fallbackPosts, {
      previewAction: "save_preview",
      previewPath: "save-preview",
      previewButton: "Preview HA to Git",
      finalAction: "save",
      finalPath: "save",
      finalButton: "Save HA to Git",
      finalText: "Harness live HA changes committed to Git.",
    });
    assert(fallbackErrors.length === 0, `fetch fallback page errors: ${fallbackErrors.join(" | ")}`);
    await fallbackContext.close();

    await harnessPost(baseUrl, "__dev_harness__/clear-previews");
    const mobilePreviewResponse = await fetch(new URL("save-preview", baseUrl), {
      method: "POST",
      headers: { Accept: "application/json", "X-Requested-With": "fetch" },
    }).then((response) => response.json());
    assert(mobilePreviewResponse.ok, `mobile preview seed failed: ${JSON.stringify(mobilePreviewResponse)}`);
    await waitFor("mobile preview seed completion", async () => {
      const state = await diagnostics(baseUrl);
      return state.state?.last_action === "save_preview" && state.state?.last_status === "success" ? state : null;
    });

    const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const mobilePage = await mobileContext.newPage();
    await mobilePage.goto(baseUrl);
    await mobilePage.getByTestId("preview-file").first().waitFor({ timeout: 5000 });
    await assertLogPanelLayout(mobilePage, "fallback", "initial mobile");
    await assertMobilePreviewUsability(mobilePage, "mobile completed preview");
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
