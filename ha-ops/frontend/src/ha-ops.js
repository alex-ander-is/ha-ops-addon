import { LitElement, css, html, nothing } from "lit";
import "@vaadin/button";
import "@vaadin/checkbox";
import "@vaadin/confirm-dialog";
import "@vaadin/progress-bar";
import "@vaadin/radio-group";
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

function highlightedDiffLines(diff) {
  return String(diff || "").split("\n").map((line) => {
    let kind = "ctx";
    if (line.startsWith("+") && !line.startsWith("+++")) kind = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) kind = "del";
    else if (line.startsWith("@@")) kind = "hunk";
    else if (line.startsWith("diff --git") || line.startsWith("---") || line.startsWith("+++")) kind = "meta";
    return html`<span class=${`line ${kind}`}>${line || " "}</span>`;
  });
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
  };
  static styles = css`
    :host { display: block; border: 1px solid var(--ha-ops-border, #d0d7de); border-radius: 8px; overflow: hidden; }
    header { display: flex; align-items: center; gap: .75rem; padding: .65rem .75rem; }
    code { min-width: 0; overflow-wrap: anywhere; }
    pre { margin: 0; padding: .75rem; overflow: auto; white-space: pre; border-top: 1px solid var(--ha-ops-border, #d0d7de); background: var(--ha-ops-code-bg, #f6f8fa); }
    .line { display: block; min-height: 1.25em; color: var(--ha-ops-code-text, #24292f); }
    .add { color: var(--ha-ops-diff-add-text, #116329); background: var(--ha-ops-diff-add-bg, #dafbe1); }
    .del { color: var(--ha-ops-diff-del-text, #82071e); background: var(--ha-ops-diff-del-bg, #ffebe9); }
    .hunk { color: var(--ha-ops-diff-hunk-text, #0550ae); background: var(--ha-ops-diff-hunk-bg, #ddf4ff); }
    .meta { color: var(--ha-ops-muted-text, #57606a); font-weight: 600; }
    [role="status"] { padding: .75rem; color: var(--ha-ops-muted-text, #57606a); }
  `;
  constructor() {
    super(); this.path = ""; this.cursor = null; this.generation = 0; this.expanded = false; this.diff = ""; this.diffState = "idle";
  }
  willUpdate(changed) {
    const cursorChanged = changed.has("cursor") && cursorKey(changed.get("cursor")) !== cursorKey(this.cursor);
    if (cursorChanged || changed.has("generation")) { this.expanded = false; this.diff = ""; this.diffState = "idle"; }
  }
  render() {
    return html`
      <header>
        <vaadin-button theme="secondary" aria-expanded=${String(this.expanded)} @click=${() => this.setExpanded(!this.expanded)}>
          ${this.expanded ? TEXT.collapse : TEXT.expand}
        </vaadin-button>
        <code>${this.path}</code>
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
}
customElements.define("ha-ops-preview-file", HaOpsPreviewFile);

class HaOpsPreview extends LitElement {
  static properties = { state: { type: Object }, direction: { type: String }, running: { type: Boolean } };
  static styles = css`
    :host { display: grid; gap: .65rem; margin-top: 1rem; }
    header { display: flex; align-items: center; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
    .actions { display: flex; gap: .5rem; }
    .files { display: grid; gap: .5rem; }
    footer { display: flex; justify-content: flex-end; }
  `;
  constructor() { super(); this.state = {}; this.direction = "apply"; this.running = false; }
  get paths() { return this.direction === "save" ? this.state.last_save_preview_paths || [] : this.state.last_preview_paths || []; }
  get cursor() { return this.direction === "save" ? this.state.last_save_diff_cursor : this.state.last_diff_cursor; }
  get finalCommand() { return this.direction === "save" ? "save" : "apply"; }
  get finalLabel() { return this.direction === "save" ? TEXT.save : TEXT.apply; }
  render() {
    if (!this.paths.length) return nothing;
    return html`
      <header>
        <h3>${this.direction === "save" ? TEXT.savePreview : TEXT.applyPreview}</h3>
        <div class="actions">
          <vaadin-button theme="secondary" @click=${() => this.setAll(true)}>${TEXT.expandAll}</vaadin-button>
          <vaadin-button theme="secondary" @click=${() => this.setAll(false)}>${TEXT.collapseAll}</vaadin-button>
        </div>
      </header>
      <div class="files">
        ${this.paths.map((path) => html`<ha-ops-preview-file
          data-testid="preview-file" .path=${path} .cursor=${this.cursor}
          .generation=${Number(this.state.operation_generation || 0)}></ha-ops-preview-file>`)}
      </div>
      <footer>
        <vaadin-button theme="primary" ?disabled=${this.running} @click=${() => this.runFinalAction()}>
          ${this.finalLabel}
        </vaadin-button>
      </footer>
    `;
  }
  setAll(expanded) { for (const file of this.renderRoot.querySelectorAll("ha-ops-preview-file")) file.setExpanded(expanded); }
  runFinalAction() {
    if (this.running) return;
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
    .connection { position: fixed; right: .75rem; bottom: .75rem; z-index: 20; padding: .35rem .6rem;
      border: 1px solid var(--ha-ops-muted-border, #9aa0a6); border-radius: 999px;
      color: var(--ha-ops-muted-text, #5f6368); background: var(--ha-ops-surface, #fff); font: 12px/1.2 system-ui; }
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
    this.shouldReconnect = false;
    if (this.socket) this.socket.close();
    super.disconnectedCallback();
  }

  render() {
    return html`
      <slot></slot>
      <section data-testid="reactive-previews">
        ${this.state.last_preview_paths?.length
          ? html`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} direction="apply"
              @ha-ops-command=${this.onCommand}></ha-ops-preview>` : nothing}
        ${this.state.last_save_preview_paths?.length
          ? html`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} direction="save"
              @ha-ops-command=${this.onCommand}></ha-ops-preview>` : nothing}
      </section>
      <vaadin-confirm-dialog
        .opened=${this.confirmOpen}
        .message=${this.confirmMessage}
        .confirmText=${TEXT.confirm}
        cancel-button-visible
        @confirm=${this.confirmMutation}
        @cancel=${() => { this.confirmOpen = false; this.confirmForm = null; }}
      ></vaadin-confirm-dialog>
      <span class="connection" role="status" data-testid="connection-status">${this.connection}</span>
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
    this.dispatchMutation(form).catch((error) => this.markUnknown(error));
  };

  onCommand = (event) => {
    event.stopPropagation();
    const { command, payload } = event.detail || {};
    this.dispatchCommand(command, new URL(command.replaceAll("_", "-"), baseUrl()).href, payload || {})
      .catch((error) => this.markUnknown(error));
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
    this.connection = "connecting";
    this.replayPending = true;
    if (typeof window.WebSocket !== "function") {
      this.socket = null;
      this.loadHttpBaseline();
      return;
    }
    const socket = new WebSocket(websocketUrl());
    this.socket = socket;
    socket.addEventListener("open", () => {
      this.connection = "replaying";
      socket.send(JSON.stringify({ id: String(this.nextRequestId++), command: "replay" }));
    });
    socket.addEventListener("message", (event) => this.receive(JSON.parse(event.data)));
    socket.addEventListener("close", () => {
      if (!this.shouldReconnect) return;
      this.connection = "reconnecting";
      for (const pending of this.pending.values()) {
        pending.reject(new Error(pending.sent ? "Command outcome is unknown after disconnect." : "WebSocket unavailable."));
      }
      this.pending.clear();
      this.reconnectTimer = setTimeout(() => this.connect(), 1200);
    });
  }

  async loadHttpBaseline() {
    try {
      const response = await fetch("debug-snapshot");
      const snapshot = await response.json();
      this.applyBaseline(snapshot);
      this.replayPending = false;
      this.connection = "http";
    } catch (error) {
      this.connection = "unknown";
      this.markUnknown(error);
    }
  }

  receive(frame) {
    if (frame.type === "ready" || frame.type === "replay") {
      this.applyBaseline(frame);
      this.replayPending = false;
      this.connection = "connected";
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
      this.connection = "replaying";
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
    const badge = this.querySelector("[data-status-code]");
    if (badge) {
      badge.dataset.statusCode = this.state.last_status || "idle";
      badge.textContent = this.state.last_status === "success" ? "done" : (this.state.last_status || "idle");
    }
    const log = this.querySelector("ha-ops-log");
    if (log) {
      const lines = Array.isArray(this.state.last_details) && this.state.last_details.length
        ? this.state.last_details : [this.state.last_message || ""];
      log.lines = lines;
      log.status = this.state.last_status || "idle";
    }
    this.upgradeControls();
  }

  isRunning() {
    return this.state.last_status === "running" || Object.values(this.state.command_records || {})
      .some((record) => ["accepted", "running", "failed_unknown"].includes(record.status));
  }

  markUnknown(error) {
    this.connection = "unknown";
    const target = this.querySelector("#client-status");
    if (target) target.textContent = error.message;
  }
}
customElements.define("ha-ops-app", HaOpsApp);
