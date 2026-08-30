import { LitElement, css, html, nothing, render } from "lit";
import "@vaadin/button";
import "@vaadin/checkbox";
import "@vaadin/confirm-dialog";
import "@vaadin/progress-bar";
import "@vaadin/radio-group";
import "@vaadin/radio-group/vaadin-radio-button.js";
import "@vaadin/select";

const MUTATING_METHOD = "post";
const TEXT = window.__HA_OPS_TEXT__ || {};
const WS_COMMANDS = new Set([
  "preview", "save_preview", "apply", "save", "select_save_preview", "select_apply_preview",
  "resolve_save_preview", "resolve_apply_preview", "reset_git_state", "disk_usage",
  "deleted_devices_preview", "retained_devices_preview", "retained_devices_delete",
  "internal_ids_preview", "internal_ids_migrate", "deleted_devices_delete",
  "deleted_devices_confirm", "deleted_devices_revert", "rollback",
]);

function sortedStrings(items) {
  return [...(items || [])].map((item) => String(item)).filter(Boolean).sort();
}

function sortedObject(value) {
  return Object.fromEntries(Object.entries(value || {}).sort(([left], [right]) => left.localeCompare(right)));
}

function cursorIdentity(cursor) {
  if (!cursor || typeof cursor !== "object") return null;
  const identity = {};
  for (const key of ["schema", "kind", "generation", "artifact", "sha256", "bytes"]) {
    if (Object.hasOwn(cursor, key)) identity[key] = cursor[key];
  }
  return identity;
}

function cursorKey(cursor) {
  return JSON.stringify(cursorIdentity(cursor));
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

function commandDirection(command) {
  if (command === "select_save_preview" || command === "resolve_save_preview") return "save";
  if (command === "select_apply_preview" || command === "resolve_apply_preview") return "apply";
  return null;
}

function diffLineKind(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  if (line.startsWith("diff --git")) return "meta";
  return "ctx";
}

function changedRanges(oldText, newText) {
  let prefixLength = 0;
  const maxPrefix = Math.min(oldText.length, newText.length);
  while (prefixLength < maxPrefix && oldText[prefixLength] === newText[prefixLength]) prefixLength += 1;

  let suffixLength = 0;
  const maxSuffix = Math.min(oldText.length, newText.length) - prefixLength;
  while (
    suffixLength < maxSuffix
    && oldText[oldText.length - suffixLength - 1] === newText[newText.length - suffixLength - 1]
  ) {
    suffixLength += 1;
  }

  return [
    [prefixLength, oldText.length - suffixLength],
    [prefixLength, newText.length - suffixLength],
  ];
}

const UNICODE_ESCAPE_RE = /\\(?:U[0-9A-Fa-f]{8}|u[0-9A-Fa-f]{4})/g;

function unicodeEscapeCharacter(value) {
  const codepoint = Number.parseInt(value.slice(2), 16);
  if (codepoint >= 0xd800 && codepoint <= 0xdfff) return null;
  try {
    return String.fromCodePoint(codepoint);
  } catch (_error) {
    return null;
  }
}

function expandChangedRangeForUnicodeEscapes(text, range) {
  let [start, end] = range;
  for (const match of text.matchAll(UNICODE_ESCAPE_RE)) {
    if (match.index < end && start < match.index + match[0].length) {
      start = Math.min(start, match.index);
      end = Math.max(end, match.index + match[0].length);
    }
  }
  return [start, end];
}

function renderDiffText(text) {
  const parts = [];
  let last = 0;
  for (const match of text.matchAll(UNICODE_ESCAPE_RE)) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const character = unicodeEscapeCharacter(match[0]);
    parts.push(character
      ? html`<span class="unicode-escape" title=${character} data-unicode-char=${character}>${match[0]}</span>`
      : match[0]);
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function renderChangedText(text, range) {
  const [start, end] = expandChangedRangeForUnicodeEscapes(text, range);
  if (start >= end) return renderDiffText(text);
  return [
    ...renderDiffText(text.slice(0, start)),
    html`<span class="diff-changed">${renderDiffText(text.slice(start, end))}</span>`,
    ...renderDiffText(text.slice(end)),
  ];
}

function renderDiffLine(line, changedRange = null) {
  const kind = diffLineKind(line);
  const content = changedRange && (kind === "add" || kind === "del")
    ? [line.slice(0, 1), ...renderChangedText(line.slice(1), changedRange)]
    : renderDiffText(line || " ");
  return html`<span class=${`line ${kind}`}>${content}</span>`;
}

function highlightedDiffLines(diff) {
  const lines = String(diff || "").split("\n");
  const rendered = [];
  let index = 0;
  while (index < lines.length) {
    const removed = [];
    const added = [];
    let blockIndex = index;
    while (blockIndex < lines.length && lines[blockIndex].startsWith("-") && !lines[blockIndex].startsWith("---")) {
      removed.push(lines[blockIndex]);
      blockIndex += 1;
    }
    while (blockIndex < lines.length && lines[blockIndex].startsWith("+") && !lines[blockIndex].startsWith("+++")) {
      added.push(lines[blockIndex]);
      blockIndex += 1;
    }
    if (removed.length || added.length) {
      const pairs = Math.min(removed.length, added.length);
      for (let pairIndex = 0; pairIndex < pairs; pairIndex += 1) {
        const [oldRange, newRange] = changedRanges(removed[pairIndex].slice(1), added[pairIndex].slice(1));
        rendered.push(renderDiffLine(removed[pairIndex], oldRange));
        rendered.push(renderDiffLine(added[pairIndex], newRange));
      }
      for (const line of removed.slice(pairs)) rendered.push(renderDiffLine(line));
      for (const line of added.slice(pairs)) rendered.push(renderDiffLine(line));
      index = blockIndex;
    } else {
      rendered.push(renderDiffLine(lines[index]));
      index += 1;
    }
  }
  return rendered;
}

function uuid() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function baseUrl() {
  const base = new URL(window.location.href);
  if (!base.pathname.endsWith("/")) {
    const slash = base.pathname.lastIndexOf("/");
    const segment = base.pathname.slice(slash + 1);
    base.pathname = segment && !segment.includes(".") ? `${base.pathname}/` : base.pathname.slice(0, slash + 1);
  }
  return base;
}

function websocketUrl() {
  const url = new URL("ws", baseUrl());
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

function commandForAction(action) {
  const name = new URL(action, window.location.href).pathname.split("/").filter(Boolean).pop() || "";
  return name.replaceAll("-", "_");
}

function formPayload(form) {
  const payload = {};
  for (const [key, value] of new FormData(form).entries()) {
    if (Object.hasOwn(payload, key)) {
      payload[key] = Array.isArray(payload[key]) ? [...payload[key], value] : [payload[key], value];
    } else {
      payload[key] = value;
    }
  }
  return payload;
}

class HaOpsLog extends LitElement {
  static properties = { lines: { type: Array }, status: { type: String } };
  static styles = css`
    :host { display: contents; }
    pre { box-sizing: border-box; height: 100%; margin: 0; overflow: auto; white-space: pre-wrap; }
  `;
  constructor() {
    super();
    this.lines = [];
    this.status = "idle";
  }
  render() {
    return html`<pre data-testid="operation-log" aria-label="Operation log">${this.lines.join("\n")}</pre>`;
  }
  firstUpdated() {
    const log = this.renderRoot.querySelector("pre");
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem("haOpsLogScrollState") || "null"); } catch (_error) {}
    requestAnimationFrame(() => {
      log.scrollTop = saved?.sticky === false ? Math.min(saved.scrollTop || 0, log.scrollHeight - log.clientHeight) : log.scrollHeight;
    });
    log.addEventListener("scroll", () => {
      const sticky = log.scrollHeight - log.scrollTop - log.clientHeight <= 4;
      sessionStorage.setItem("haOpsLogScrollState", JSON.stringify({ sticky, scrollTop: log.scrollTop }));
    }, { passive: true });
  }
  updated() {
    const log = this.renderRoot.querySelector("pre");
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem("haOpsLogScrollState") || "null"); } catch (_error) {}
    if (!saved || saved.sticky !== false) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  }
}
customElements.define("ha-ops-log", HaOpsLog);

class HaOpsPreviewFile extends LitElement {
  static properties = {
    path: { type: String }, cursor: { type: Object }, generation: { type: Number },
    expanded: { type: Boolean }, diff: { type: String }, diffState: { type: String },
    selected: { type: Boolean }, choice: { type: String }, conflict: { type: Boolean },
    direction: { type: String }, running: { type: Boolean },
  };
  static styles = css`
    :host { display: block; border: 1px solid var(--ha-ops-border, #d0d7de); border-radius: 8px; overflow: hidden; }
    header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: .65rem; padding: .65rem .75rem; }
    code { min-width: 0; overflow-wrap: anywhere; }
    .path { min-width: 0; display: flex; align-items: center; gap: .5rem; }
    .choice { display: flex; justify-content: flex-end; min-width: 0; }
    vaadin-radio-group { width: 100%; max-width: 100%; }
    vaadin-radio-group::part(group-field) { display: flex; flex-wrap: wrap; gap: .25rem .75rem; }
    vaadin-radio-button { max-width: 100%; }
    vaadin-radio-button::part(label) { white-space: normal; overflow-wrap: anywhere; }
    pre { margin: 0; padding: .75rem; overflow: auto; white-space: pre; border-top: 1px solid var(--ha-ops-border, #d0d7de); background: var(--ha-ops-code-bg, #f6f8fa); }
    .line { display: block; min-height: 1.25em; color: var(--ha-ops-code-text, #24292f); }
    .add { color: var(--ha-ops-diff-add-text, #116329); background: var(--ha-ops-diff-add-bg, #dafbe1); }
    .del { color: var(--ha-ops-diff-del-text, #82071e); background: var(--ha-ops-diff-del-bg, #ffebe9); }
    .diff-changed { border-radius: 3px; padding: 0 1px; font-weight: 700; }
    .add .diff-changed { background: color-mix(in srgb, var(--ha-ops-diff-add-text, #116329) 24%, transparent); }
    .del .diff-changed { background: color-mix(in srgb, var(--ha-ops-diff-del-text, #82071e) 20%, transparent); }
    .unicode-escape { border-bottom: 1px dotted currentColor; cursor: help; }
    .hunk { color: var(--ha-ops-diff-hunk-text, #0550ae); background: var(--ha-ops-diff-hunk-bg, #ddf4ff); }
    .meta { color: var(--ha-ops-muted-text, #57606a); font-weight: 600; }
    [role="status"] { padding: .75rem; color: var(--ha-ops-muted-text, #57606a); }
    @media (max-width: 700px) {
      header { grid-template-columns: minmax(0, 1fr); align-items: stretch; }
      .path, .choice { justify-content: flex-start; }
      .path { flex-wrap: wrap; }
      vaadin-button { width: fit-content; }
    }
  `;
  constructor() {
    super();
    this.path = "";
    this.cursor = null;
    this.generation = 0;
    this.expanded = false;
    this.diff = "";
    this.diffState = "idle";
    this.selected = false;
    this.choice = "";
    this.conflict = false;
    this.direction = "apply";
    this.running = false;
  }
  willUpdate(changed) {
    const cursorChanged = changed.has("cursor") && cursorKey(changed.get("cursor")) !== cursorKey(this.cursor);
    const pathChanged = changed.has("path") && changed.get("path") !== this.path;
    if (cursorChanged || changed.has("generation") || pathChanged) { this.expanded = false; this.diff = ""; this.diffState = "idle"; }
  }
  render() {
    return html`
      <header>
        <div class="path">
          <vaadin-checkbox
            aria-label=${`${TEXT.includeFile || "Include file"} ${this.path}`}
            .checked=${this.selected}
            ?disabled=${this.running}
            @change=${this.onSelectChange}></vaadin-checkbox>
          <code>${this.path}</code>
        </div>
        <vaadin-button theme="secondary" ?disabled=${this.running} aria-expanded=${String(this.expanded)} @click=${() => this.setExpanded(!this.expanded)}>
          ${this.expanded ? TEXT.collapse : TEXT.expand}
        </vaadin-button>
        <div class="choice">
          <vaadin-radio-group
            aria-label=${`${TEXT.versionChoice || "Version choice"} ${this.path}`}
            .value=${this.choice || ""}
            ?disabled=${this.running || !this.selected}
            @change=${this.onChoiceChange}>
            <vaadin-radio-button value="git">${TEXT.useGitVersion}</vaadin-radio-button>
            <vaadin-radio-button value="ha">${TEXT.useHaVersion}</vaadin-radio-button>
          </vaadin-radio-group>
        </div>
      </header>
      ${this.expanded ? this.diffState === "loaded"
        ? html`<pre aria-label="Diff detail">${highlightedDiffLines(this.diff)}</pre>`
        : html`<div role="status">${this.diffState === "stale" ? TEXT.unavailableDiff : TEXT.loadingDiff}</div>`
        : nothing}
    `;
  }
  async setExpanded(expanded) {
    this.expanded = expanded;
    if (!expanded || this.diffState === "loaded") return;
    this.diffState = "loading";
    try {
      const response = await fetch(`diff-get?cursor=${encodeURIComponent(JSON.stringify(this.cursor))}&path=${encodeURIComponent(this.path)}`);
      const payload = await response.json();
      if (!payload.ok || Number(this.cursor?.generation) !== Number(this.generation)) throw new Error("stale");
      this.diff = payload.diff;
      this.diffState = "loaded";
    } catch (_error) {
      this.diff = "";
      this.diffState = "stale";
    }
  }
  onSelectChange = (event) => {
    this.dispatchEvent(new CustomEvent("preview-select", {
      bubbles: true,
      composed: true,
      detail: { path: this.path, selected: event.target.checked },
    }));
  };
  onChoiceChange = (event) => {
    const choice = event.detail?.value || event.target?.value || "";
    if (!choice) return;
    this.dispatchEvent(new CustomEvent("preview-resolve", {
      bubbles: true,
      composed: true,
      detail: { path: this.path, choice },
    }));
  };
}
customElements.define("ha-ops-preview-file", HaOpsPreviewFile);

class HaOpsPreview extends LitElement {
  static properties = { state: { type: Object }, direction: { type: String }, running: { type: Boolean } };
  static styles = css`
    :host { display: grid; gap: .65rem; margin-top: 1rem; }
    header { display: flex; align-items: center; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
    .actions { display: flex; gap: .5rem; flex-wrap: wrap; }
    .files { display: grid; gap: .5rem; }
    footer { display: flex; justify-content: flex-end; }
  `;
  constructor() { super(); this.state = {}; this.direction = "apply"; this.running = false; }
  get paths() { return this.direction === "save" ? this.state.last_save_preview_paths || [] : this.state.last_preview_paths || []; }
  get cursor() { return this.direction === "save" ? this.state.last_save_diff_cursor : this.state.last_diff_cursor; }
  get selectedPaths() { return this.direction === "save" ? this.state.save_preview_selected_paths || [] : this.state.apply_preview_selected_paths || []; }
  get resolutions() { return this.direction === "save" ? this.state.save_preview_resolutions || {} : this.state.apply_preview_resolutions || {}; }
  get conflictPaths() { return this.direction === "save" ? this.state.last_save_preview_conflict_paths || [] : this.state.last_preview_conflict_paths || []; }
  get finalCommand() { return this.direction === "save" ? "save" : "apply"; }
  get finalLabel() { return this.direction === "save" ? TEXT.save : TEXT.apply; }
  get selectCommand() { return this.direction === "save" ? "select_save_preview" : "select_apply_preview"; }
  get resolveCommand() { return this.direction === "save" ? "resolve_save_preview" : "resolve_apply_preview"; }
  isSelected(path) { return new Set(this.selectedPaths).has(path); }
  isConflict(path) { return new Set(this.conflictPaths).has(path); }
  choiceFor(path) { return this.resolutions[path] || ""; }
  effectiveChoice(path) {
    const explicit = this.choiceFor(path);
    if (explicit) return explicit;
    if (this.direction === "save" && this.isConflict(path) && this.isSelected(path)) return "";
    return this.direction === "save" ? "ha" : "git";
  }
  selectedConflictChoicesMissing() {
    if (this.direction !== "save") return false;
    const selected = new Set(this.selectedPaths);
    return this.conflictPaths.some((path) => selected.has(path) && !this.resolutions[path]);
  }
  isFinalActionDisabled() {
    return this.running || !this.selectedPaths.length || this.selectedConflictChoicesMissing();
  }
  render() {
    if (!this.paths.length) return nothing;
    return html`
      <header>
        <h3>${this.direction === "save" ? TEXT.savePreview : TEXT.applyPreview}</h3>
        <div class="actions">
          <vaadin-button theme="secondary" ?disabled=${this.running} @click=${() => this.selectAll(true)}>${TEXT.selectAll}</vaadin-button>
          <vaadin-button theme="secondary" ?disabled=${this.running} @click=${() => this.selectAll(false)}>${TEXT.selectNone}</vaadin-button>
          <vaadin-button theme="secondary" @click=${() => this.setAll(true)}>${TEXT.expandAll}</vaadin-button>
          <vaadin-button theme="secondary" @click=${() => this.setAll(false)}>${TEXT.collapseAll}</vaadin-button>
        </div>
      </header>
      <div class="files">
        ${this.paths.map((path) => html`<ha-ops-preview-file
          data-testid="preview-file" .path=${path} .cursor=${this.cursor}
          .generation=${Number(this.state.operation_generation || 0)}
          .direction=${this.direction}
          .running=${this.running}
          .selected=${this.isSelected(path)}
          .conflict=${this.isConflict(path)}
          .choice=${this.effectiveChoice(path)}
          @preview-select=${this.onPreviewSelect}
          @preview-resolve=${this.onPreviewResolve}></ha-ops-preview-file>`)}
      </div>
      <footer>
        <vaadin-button theme="primary" ?disabled=${this.isFinalActionDisabled()} @click=${() => this.runFinalAction()}>
          ${this.finalLabel}
        </vaadin-button>
      </footer>
    `;
  }
  setAll(expanded) { for (const file of this.renderRoot.querySelectorAll("ha-ops-preview-file")) file.setExpanded(expanded); }
  selectAll(selected) {
    if (this.running) return;
    this.dispatchEvent(new CustomEvent("ha-ops-command", {
      bubbles: true,
      composed: true,
      detail: {
        command: this.selectCommand,
        payload: { selection_action: selected ? "all" : "none", preview_identity: previewIdentity(this.state, this.direction) },
      },
    }));
  }
  onPreviewSelect = (event) => {
    event.stopPropagation();
    if (this.running) return;
    this.dispatchEvent(new CustomEvent("ha-ops-command", {
      bubbles: true,
      composed: true,
      detail: {
        command: this.selectCommand,
        payload: {
          path: event.detail.path,
          selected: event.detail.selected ? "1" : "",
          preview_identity: previewIdentity(this.state, this.direction),
        },
      },
    }));
  };
  onPreviewResolve = (event) => {
    event.stopPropagation();
    if (this.running) return;
    this.dispatchEvent(new CustomEvent("ha-ops-command", {
      bubbles: true,
      composed: true,
      detail: {
        command: this.resolveCommand,
        payload: {
          path: event.detail.path,
          choice: event.detail.choice,
          preview_identity: previewIdentity(this.state, this.direction),
        },
      },
    }));
  };
  runFinalAction() {
    if (this.isFinalActionDisabled()) return;
    this.dispatchEvent(new CustomEvent("ha-ops-command", {
      bubbles: true,
      composed: true,
      detail: { command: this.finalCommand, payload: {} },
    }));
  }
}
customElements.define("ha-ops-preview", HaOpsPreview);

class HaOpsApp extends LitElement {
  static properties = {
    connection: { type: String },
    revision: { type: Number },
    state: { type: Object },
    confirmOpen: { type: Boolean },
    confirmMessage: { type: String },
  };

  static styles = css`
    :host { display: contents; }
  `;

  constructor() {
    super();
    this.connection = "connecting";
    this.revision = 0;
    this.state = {};
    this.confirmOpen = false;
    this.confirmMessage = "";
    this.confirmForm = null;
    this.socket = null;
    this.pending = new Map();
    this.nextRequestId = 1;
    this.reconnectTimer = null;
    this.reconnectStableTimer = null;
    this.reconnectDelayMs = 1200;
    this.replayPending = true;
    this.queuedFrames = [];
    this.shouldReconnect = false;
  }

  connectedCallback() {
    super.connectedCallback();
    this.addEventListener("submit", this.onSubmit);
    this.upgradeControls();
    this.observeLayout();
    this.shouldReconnect = true;
    this.connect();
    if (window.__HA_OPS_ENABLE_TEST_HOOKS__ === true) window.__haOpsTestCloseWs = () => this.socket?.close();
  }

  disconnectedCallback() {
    this.removeEventListener("submit", this.onSubmit);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.reconnectStableTimer) clearTimeout(this.reconnectStableTimer);
    this.shouldReconnect = false;
    if (this.socket) this.socket.close();
    super.disconnectedCallback();
  }

  render() {
    return html`
      <slot></slot>
      <vaadin-confirm-dialog
        .opened=${this.confirmOpen}
        .message=${this.confirmMessage}
        .confirmText=${TEXT.confirm}
        cancel-button-visible
        @confirm=${this.confirmMutation}
        @cancel=${() => { this.confirmOpen = false; this.confirmForm = null; }}
      ></vaadin-confirm-dialog>
    `;
  }

  upgradeControls() {
    for (const button of this.querySelectorAll("button:not([data-vaadin-upgraded])")) {
      const replacement = document.createElement("vaadin-button");
      replacement.textContent = button.textContent;
      replacement.disabled = button.disabled;
      replacement.className = button.className;
      if (button.disabled) replacement.setAttribute("data-server-disabled", "true");
      replacement.setAttribute("data-vaadin-upgraded", "true");
      replacement.setAttribute("role", "button");
      if (button.classList.contains("secondary")) replacement.setAttribute("theme", "secondary");
      else replacement.setAttribute("theme", "primary");
      for (const attribute of button.attributes) {
        if (!["class", "type", "disabled"].includes(attribute.name)) replacement.setAttribute(attribute.name, attribute.value);
      }
      replacement.addEventListener("click", () => {
        if (replacement.disabled) return;
        if (button.type === "submit") replacement.closest("form")?.requestSubmit();
        else this.handleButton(replacement);
      });
      button.replaceWith(replacement);
    }
    for (const input of this.querySelectorAll('input[type="checkbox"]:not([data-vaadin-upgraded])')) {
      const checkbox = document.createElement("vaadin-checkbox");
      checkbox.name = input.name;
      checkbox.value = input.value;
      checkbox.checked = input.checked;
      checkbox.disabled = input.disabled;
      checkbox.setAttribute("data-vaadin-upgraded", "true");
      checkbox.setAttribute("aria-label", input.closest("label")?.innerText.trim() || input.name || "Selection");
      if (input.disabled) checkbox.setAttribute("data-server-disabled", "true");
      checkbox.addEventListener("change", () => {
        input.checked = checkbox.checked;
        const form = checkbox.closest("form[data-auto-submit='change']");
        if (form) form.requestSubmit();
      });
      input.replaceWith(checkbox);
    }
    for (const select of this.querySelectorAll("select:not([data-vaadin-upgraded])")) {
      const control = document.createElement("vaadin-select");
      control.name = select.name;
      control.value = select.value;
      control.items = Array.from(select.options).map((option) => ({ label: option.textContent, value: option.value }));
      control.disabled = select.disabled;
      control.setAttribute("data-vaadin-upgraded", "true");
      control.setAttribute("aria-label", select.closest("label")?.innerText.trim() || select.name || "Selection");
      if (select.disabled) control.setAttribute("data-server-disabled", "true");
      control.addEventListener("change", () => control.closest("form[data-auto-submit='change']")?.requestSubmit());
      select.replaceWith(control);
    }
  }

  handleButton(button) {
    if (button.dataset.checkboxScope) {
      const checked = button.dataset.checkboxAction === "all";
      for (const input of this.querySelectorAll(`[data-checkbox-scope="${button.dataset.checkboxScope}"] input[type="checkbox"]`)) {
        if (!input.disabled) input.checked = checked;
      }
      return;
    }
  }

  observeLayout() {
    const controls = this.querySelector(".control-card");
    const details = this.querySelector(".details-card");
    if (!controls || !details) return;
    const sync = () => {
      const sameRow = Math.abs(controls.getBoundingClientRect().top - details.getBoundingClientRect().top) < 2;
      if (sameRow) details.style.setProperty("--details-card-height", `${controls.getBoundingClientRect().height}px`);
      else details.style.removeProperty("--details-card-height");
    };
    this.resizeObserver = new ResizeObserver(sync);
    this.resizeObserver.observe(controls);
    window.addEventListener("resize", sync);
    requestAnimationFrame(sync);
  }

  onSubmit = (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== MUTATING_METHOD) return;
    event.preventDefault();
    const confirmText = form.dataset.confirm;
    if (confirmText && form.dataset.confirmed !== "true") {
      this.confirmForm = form;
      this.confirmMessage = confirmText;
      this.confirmOpen = true;
      return;
    }
    delete form.dataset.confirmed;
    this.dispatchMutation(form).catch((error) => this.handleCommandError(error));
  };

  onCommand = (event) => {
    event.stopPropagation();
    const { command, payload } = event.detail || {};
    this.dispatchCommand(command, new URL(command.replaceAll("_", "-"), baseUrl()).href, payload || {})
      .catch((error) => this.handleCommandError(error));
  };

  confirmMutation = () => {
    const form = this.confirmForm;
    this.confirmOpen = false;
    this.confirmForm = null;
    if (form) {
      form.dataset.confirmed = "true";
      form.requestSubmit();
    }
  };

  async dispatchMutation(form) {
    const command = commandForAction(form.action);
    const payload = formPayload(form);
    const direction = commandDirection(command);
    if (direction) payload.preview_identity = previewIdentity(this.state, direction);
    return this.dispatchCommand(command, form.action, payload);
  }

  async dispatchCommand(command, action, payload = {}) {
    const envelope = {
      command_id: uuid(),
      command,
      generation: Number(this.state.operation_generation || 0),
      payload,
    };
    const socket = this.socket;
    if (WS_COMMANDS.has(command) && socket && socket.readyState === window.WebSocket.OPEN && !this.replayPending) {
      const id = String(this.nextRequestId++);
      const result = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject, sent: false }));
      const entry = this.pending.get(id);
      socket.send(JSON.stringify({ id, ...envelope }));
      entry.sent = true;
      const response = await result;
      if (!response.ok) throw new Error(response.message || "Command rejected");
      return response;
    }
    if (socket && socket.readyState !== window.WebSocket?.CLOSED) {
      throw new Error("Connection state is unknown; the command was not retried.");
    }
    const response = await fetch(action, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify(envelope),
    });
    const resultPayload = await response.json();
    if (!response.ok || !resultPayload.ok) throw new Error(resultPayload.message || "Command rejected");
    await this.pollHttpCommand(envelope.command_id);
    return resultPayload;
  }

  async pollHttpCommand(commandId) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      await this.loadHttpBaseline();
      const status = this.state.command_records?.[commandId]?.status;
      if (status === "terminal") return;
      if (status === "failed_unknown") throw new Error("Command outcome is unknown.");
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error("Command did not finish before the HTTP fallback timeout.");
  }

  connect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.setConnection("connecting");
    this.replayPending = true;
    if (typeof window.WebSocket !== "function") {
      this.socket = null;
      this.loadHttpBaseline();
      return;
    }
    const socket = new WebSocket(websocketUrl());
    this.socket = socket;
    socket.addEventListener("open", () => {
      this.setConnection("replaying");
      socket.send(JSON.stringify({ id: String(this.nextRequestId++), command: "replay" }));
    });
    socket.addEventListener("message", (event) => this.receive(JSON.parse(event.data)));
    socket.addEventListener("close", () => {
      if (!this.shouldReconnect) return;
      this.setConnection("reconnecting");
      if (this.reconnectStableTimer) clearTimeout(this.reconnectStableTimer);
      for (const pending of this.pending.values()) {
        pending.reject(new Error(pending.sent ? "Command outcome is unknown after disconnect." : "WebSocket unavailable."));
      }
      this.pending.clear();
      const delay = this.reconnectDelayMs;
      this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, 30000);
      this.reconnectTimer = setTimeout(() => this.connect(), delay);
    });
  }

  async loadHttpBaseline() {
    try {
      const response = await fetch("debug-snapshot");
      const snapshot = await response.json();
      this.applyBaseline(snapshot);
      this.replayPending = false;
      this.setConnection("http");
    } catch (error) {
      this.setConnection("unknown");
      this.markUnknown(error);
    }
  }

  receive(frame) {
    if (frame.type === "ready" || frame.type === "replay") {
      this.applyBaseline(frame);
      this.replayPending = false;
      this.setConnection("connected");
      if (this.reconnectStableTimer) clearTimeout(this.reconnectStableTimer);
      this.reconnectStableTimer = setTimeout(() => {
        this.reconnectDelayMs = 1200;
        this.reconnectStableTimer = null;
      }, 10000);
      for (const queued of this.queuedFrames.splice(0)) this.receive(queued);
      return;
    }
    if (this.replayPending && ["state_patch", "log_line", "command_status"].includes(frame.type)) {
      this.queuedFrames.push(frame);
      return;
    }
    if (frame.type === "state_patch") this.applyPatch(frame);
    if (frame.type === "state") this.applyBaseline(frame);
    if (frame.type === "result" && frame.id && this.pending.has(frame.id)) {
      const pending = this.pending.get(frame.id);
      this.pending.delete(frame.id);
      pending.resolve(frame);
    }
  }

  applyBaseline(frame) {
    if (!frame.state) return;
    this.state = structuredClone(frame.state);
    this.revision = Number(frame.revision ?? frame.state_revision ?? frame.state.state_revision ?? 0);
    this.syncDom();
  }

  applyPatch(frame) {
    const base = Number(frame.base_revision);
    const revision = Number(frame.revision);
    if (revision <= this.revision) return;
    if (base !== this.revision) {
      this.replayPending = true;
      this.setConnection("replaying");
      this.socket?.send(JSON.stringify({ id: String(this.nextRequestId++), command: "replay" }));
      return;
    }
    this.state = { ...this.state, ...(frame.patch || {}) };
    this.revision = revision;
    this.syncDom();
  }

  syncDom() {
    const running = this.state.last_status === "running" || Object.values(this.state.command_records || {})
      .some((record) => ["accepted", "running", "failed_unknown"].includes(record.status));
    for (const control of this.querySelectorAll("vaadin-button, vaadin-checkbox, vaadin-radio-group, vaadin-select")) {
      if (!control.matches("[data-read-only-control]")) control.disabled = running || control.hasAttribute("data-server-disabled");
    }
    this.updateStatusBadge();
    const log = this.querySelector("ha-ops-log");
    if (log) {
      const lines = Array.isArray(this.state.last_details) && this.state.last_details.length
        ? this.state.last_details : [this.state.last_message || ""];
      log.lines = lines;
      log.status = this.state.last_status || "idle";
    }
    this.upgradeControls();
    this.syncPreviewMount();
  }

  isRunning() {
    return this.state.last_status === "running" || Object.values(this.state.command_records || {})
      .some((record) => ["accepted", "running", "failed_unknown"].includes(record.status));
  }

  isPreviewGenerationRunning() {
    const runningStatuses = new Set(["accepted", "running", "failed_unknown"]);
    if (["preview", "save_preview"].includes(this.state.last_action) && this.state.last_status === "running") return true;
    return Object.values(this.state.command_records || {})
      .some((record) => ["preview", "save_preview"].includes(record.command) && runningStatuses.has(record.status));
  }

  previewHost() {
    let host = this.querySelector("#reactive-previews[data-testid='reactive-previews']");
    if (host) return host;
    host = document.createElement("div");
    host.id = "reactive-previews";
    host.dataset.testid = "reactive-previews";
    const sections = Array.from(this.querySelectorAll("section.card.wide"));
    const gitAccess = sections.find((section) => section.querySelector("h2")?.textContent?.trim() === (TEXT.gitAccess || "Git Access"));
    gitAccess?.parentNode?.insertBefore(host, gitAccess);
    return host;
  }

  syncPreviewMount() {
    const host = this.previewHost();
    if (!host) return;
    const hasApplyPaths = Boolean(this.state.last_preview_paths?.length);
    const hasSavePaths = Boolean(this.state.last_save_preview_paths?.length);
    const previewRunning = this.isPreviewGenerationRunning();
    const visible = hasApplyPaths || hasSavePaths || previewRunning;
    if (!visible) {
      render(nothing, host);
      return;
    }
    const loading = previewRunning && !hasApplyPaths && !hasSavePaths;
    render(html`
      <section class="card wide" data-testid="diff-section">
        <h2>${TEXT.changeList}</h2>
        ${loading
          ? html`<div role="status">${TEXT.loadingPreviewDiff || "Loading Diff..."}</div>`
          : html`
              ${hasApplyPaths ? html`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} direction="apply"
                @ha-ops-command=${this.onCommand}></ha-ops-preview>` : nothing}
              ${hasSavePaths ? html`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} direction="save"
                @ha-ops-command=${this.onCommand}></ha-ops-preview>` : nothing}
            `}
      </section>
    `, host);
  }

  markUnknown(error) {
    this.setConnection("unknown");
    const target = this.querySelector("#client-status");
    if (target) target.textContent = error.message;
  }

  handleCommandError(error) {
    const message = error?.message || String(error);
    if (
      message.includes("unknown")
      || message.includes("Connection state")
      || message.includes("WebSocket unavailable")
      || message.includes("disconnect")
    ) {
      this.markUnknown(new Error(message));
      return;
    }
    const target = this.querySelector("#client-status");
    if (target) target.textContent = message;
    this.updateStatusBadge();
  }

  setConnection(connection) {
    this.connection = connection;
    this.updateStatusBadge();
  }

  isDegradedConnection() {
    return ["reconnecting", "http", "unknown"].includes(this.connection);
  }

  updateStatusBadge() {
    const badge = this.querySelector("[data-status-code]");
    if (!badge) return;
    const status = this.state.last_status || "idle";
    badge.dataset.connectionState = this.connection;
    if (this.connection === "unknown" || (status === "idle" && this.isDegradedConnection())) {
      badge.dataset.statusCode = "transport";
      badge.textContent = this.connection;
      badge.className = "badge transport";
      return;
    }
    badge.dataset.statusCode = status;
    badge.textContent = status === "success" ? "done" : status;
    badge.className = `badge ${status === "success" ? "" : status}`.trim();
  }
}
customElements.define("ha-ops-app", HaOpsApp);
