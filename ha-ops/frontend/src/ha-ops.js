import { LitElement, css, html, nothing, render } from "lit";
import "@vaadin/button";
import "@vaadin/checkbox";
import "@vaadin/confirm-dialog";
import "@vaadin/details";
import "@vaadin/progress-bar";
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

function knownVersion(value) {
  const version = String(value || "").trim();
  return Boolean(version) && version !== "unknown";
}

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
  const staticKind = {
    add: "diff-add",
    del: "diff-del",
    hunk: "diff-hunk",
    meta: "diff-file",
    ctx: "diff-context",
  }[kind];
  const content = changedRange && (kind === "add" || kind === "del")
    ? [line.slice(0, 1), ...renderChangedText(line.slice(1), changedRange)]
    : renderDiffText(line || " ");
  return html`<span class=${`line ${kind} diff-line ${staticKind}`}>${content}</span>`;
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
    direction: { type: String }, running: { type: Boolean }, wrapLines: { type: Boolean },
  };
  static styles = css`
    :host { display: block; min-width: 0; max-width: 100%; }
    vaadin-details { border: 1px solid var(--ha-ops-border, #d0d7de); border-radius: 8px; overflow: hidden; min-width: 0; max-width: 100%; }
    vaadin-details::part(content) { min-width: 0; max-width: 100%; overflow: hidden; }
    vaadin-details-summary { width: 100%; }
    vaadin-details-summary::part(content) { min-width: 0; width: 100%; max-width: 100%; }
    .summary-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: .65rem; width: 100%; max-width: 100%; min-width: 0; }
    code { min-width: 0; overflow-wrap: anywhere; }
    .path { min-width: 0; display: flex; align-items: center; gap: .5rem; }
    vaadin-checkbox::part(label) { white-space: normal; overflow-wrap: anywhere; }
    .choice { display: flex; justify-content: flex-end; gap: .35rem; min-width: 0; flex-wrap: wrap; }
    .choice vaadin-button[aria-pressed="true"] { font-weight: 700; }
    pre { box-sizing: border-box; width: 100%; max-width: 100%; min-width: 0; margin: 0; padding: .75rem; overflow-x: auto; overflow-y: auto; white-space: pre; border-top: 1px solid var(--ha-ops-border, #d0d7de); background: var(--ha-ops-code-bg, #f6f8fa); }
    pre.wrap-lines { white-space: pre-wrap; overflow-wrap: anywhere; }
    .line { display: block; width: max-content; min-width: 100%; min-height: 1.25em; color: var(--ha-ops-code-text, #24292f); }
    pre.wrap-lines .line { width: auto; min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
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
      .summary-row { grid-template-columns: minmax(0, 1fr); align-items: stretch; }
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
    this.wrapLines = true;
  }
  willUpdate(changed) {
    const cursorChanged = changed.has("cursor") && cursorKey(changed.get("cursor")) !== cursorKey(this.cursor);
    const pathChanged = changed.has("path") && changed.get("path") !== this.path;
    if (cursorChanged || changed.has("generation") || pathChanged) { this.expanded = false; this.diff = ""; this.diffState = "idle"; }
  }
  render() {
    return html`
      <vaadin-details
        .opened=${this.expanded}
        ?disabled=${this.running}
        @opened-changed=${this.onOpenedChanged}>
        <vaadin-details-summary slot="summary" aria-label=${`${this.path} ${this.expanded ? TEXT.collapse : TEXT.expand}`}>
          <div class="summary-row">
            <div class="path">
              <vaadin-checkbox
                label=${TEXT.includeFile || "Include file"}
                aria-label=${`${TEXT.includeFile || "Include file"} ${this.path}`}
                .checked=${this.selected}
                ?disabled=${this.running}
                @click=${this.stopTogglePropagation}
                @keydown=${this.stopKeyboardTogglePropagation}
                @change=${this.onSelectChange}></vaadin-checkbox>
              <code>${this.path}</code>
            </div>
            <div
              class="choice"
              role="group"
              aria-label=${`${TEXT.versionChoice || "Version choice"} ${this.path}`}
              @click=${this.stopTogglePropagation}
              @keydown=${this.stopKeyboardTogglePropagation}>
              <vaadin-button
                theme="secondary small"
                aria-pressed=${String(this.wrapLines)}
                @click=${this.onWrapToggle}>
                ${this.wrapLines ? TEXT.unwrapLines || "Unwrap Lines" : TEXT.wrapLines || "Wrap Lines"}
              </vaadin-button>
              ${this.choiceButton("ha", TEXT.useHaVersion)}
              ${this.choiceButton("git", TEXT.useGitVersion)}
            </div>
          </div>
        </vaadin-details-summary>
        ${this.expanded ? this.diffState === "loaded"
          ? html`<pre class=${this.wrapLines ? "wrap-lines" : ""} aria-label="Diff detail">${highlightedDiffLines(this.diff)}</pre>`
          : html`<div role="status">${this.diffState === "stale" ? TEXT.unavailableDiff : TEXT.loadingDiff}</div>`
          : nothing}
      </vaadin-details>
    `;
  }
  choiceButton(choice, label) {
    const pressed = this.choice === choice;
    return html`
      <vaadin-button
        theme=${pressed ? "primary small" : "secondary small"}
        aria-pressed=${String(pressed)}
        ?disabled=${this.running || !this.selected}
        @click=${() => this.dispatchChoice(choice)}>
        ${label}
      </vaadin-button>
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
  onOpenedChanged = (event) => {
    this.setExpanded(Boolean(event.detail?.value));
  };
  stopTogglePropagation = (event) => {
    event.stopPropagation();
  };
  stopKeyboardTogglePropagation = (event) => {
    if (event.key === "Enter" || event.key === " ") event.stopPropagation();
  };
  onSelectChange = (event) => {
    this.dispatchEvent(new CustomEvent("preview-select", {
      bubbles: true,
      composed: true,
      detail: { path: this.path, selected: event.target.checked },
    }));
  };
  onWrapToggle = (event) => {
    event.stopPropagation();
    this.dispatchEvent(new CustomEvent("preview-wrap-toggle", {
      bubbles: true,
      composed: true,
      detail: { path: this.path, wrapLines: !this.wrapLines },
    }));
  };
  dispatchChoice(choice) {
    if (!choice || this.running || !this.selected) return;
    this.dispatchEvent(new CustomEvent("preview-resolve", {
      bubbles: true,
      composed: true,
      detail: { path: this.path, choice },
    }));
  }
}
customElements.define("ha-ops-preview-file", HaOpsPreviewFile);

class HaOpsPreview extends LitElement {
  static properties = {
    state: { type: Object },
    direction: { type: String },
    running: { type: Boolean },
    wrapByPath: { state: true },
    previewIdentityKey: { state: true },
    commitSubject: { state: true },
    defaultCommitSubject: { state: true },
    commitSubjectPreviewIdentityKey: { state: true },
  };
  static styles = css`
    :host { display: grid; gap: .65rem; margin-top: 1rem; min-width: 0; max-width: 100%; }
    header { display: flex; align-items: center; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
    .actions { display: flex; gap: .5rem; flex-wrap: wrap; }
    .files { display: grid; gap: .5rem; min-width: 0; max-width: 100%; }
    footer { display: block; min-width: 0; max-width: 100%; }
    .footer-actions { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: .5rem; min-width: 0; max-width: 100%; width: 100%; }
    .footer-actions.apply-only { display: flex; justify-content: flex-end; }
    .commit-subject-label { color: var(--ha-ops-muted-text, #57606a); font-size: .95rem; white-space: nowrap; }
    input.commit-subject { box-sizing: border-box; width: 100%; min-width: 0; max-width: 100%; border: 1px solid var(--ha-ops-border, #d0d7de); border-radius: 6px; padding: .45rem .55rem; font: inherit; color: var(--ha-ops-text, #24292f); background: var(--ha-ops-surface, #ffffff); }
    input.commit-subject:disabled { color: var(--ha-ops-disabled-text, #8c959f); background: var(--ha-ops-disabled-bg, #f6f8fa); border-color: var(--ha-ops-disabled-border, #d8dee4); opacity: 1; }
    @media (max-width: 700px) {
      header { align-items: stretch; }
      .actions { justify-content: flex-start; }
      .footer-actions { gap: .4rem; }
    }
  `;
  constructor() {
    super();
    this.state = {};
    this.direction = "apply";
    this.running = false;
    this.wrapByPath = {};
    this.previewIdentityKey = "";
    this.commitSubject = "";
    this.defaultCommitSubject = "";
    this.commitSubjectPreviewIdentityKey = "";
  }
  get paths() { return this.direction === "save" ? this.state.last_save_preview_paths || [] : this.state.last_preview_paths || []; }
  get cursor() { return this.direction === "save" ? this.state.last_save_diff_cursor : this.state.last_diff_cursor; }
  get selectedPaths() { return this.direction === "save" ? this.state.save_preview_selected_paths || [] : this.state.apply_preview_selected_paths || []; }
  get resolutions() { return this.direction === "save" ? this.state.save_preview_resolutions || {} : this.state.apply_preview_resolutions || {}; }
  get conflictPaths() { return this.direction === "save" ? this.state.last_save_preview_conflict_paths || [] : this.state.last_preview_conflict_paths || []; }
  get finalCommand() { return this.direction === "save" ? "save" : "apply"; }
  get finalLabel() { return this.direction === "save" ? TEXT.save : TEXT.apply; }
  get selectCommand() { return this.direction === "save" ? "select_save_preview" : "select_apply_preview"; }
  get resolveCommand() { return this.direction === "save" ? "resolve_save_preview" : "resolve_apply_preview"; }
  willUpdate() {
    const identityKey = JSON.stringify(previewIdentity(this.state, this.direction));
    if (identityKey !== this.previewIdentityKey) {
      this.previewIdentityKey = identityKey;
      this.wrapByPath = {};
    }
    if (this.direction === "save" && identityKey !== this.commitSubjectPreviewIdentityKey) {
      this.commitSubjectPreviewIdentityKey = identityKey;
      this.defaultCommitSubject = this.state.last_save_commit_subject || "";
      this.commitSubject = this.defaultCommitSubject;
    }
  }
  isSelected(path) { return new Set(this.selectedPaths).has(path); }
  isConflict(path) { return new Set(this.conflictPaths).has(path); }
  isWrapped(path) { return this.wrapByPath[path] !== false; }
  allCurrentPathsWrapped() { return this.paths.length > 0 && this.paths.every((path) => this.isWrapped(path)); }
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
          <vaadin-button theme="secondary" @click=${() => this.wrapAll(!this.allCurrentPathsWrapped())}>
            ${this.allCurrentPathsWrapped() ? TEXT.unwrapAllLines || "Unwrap All Lines" : TEXT.wrapAllLines || "Wrap All Lines"}
          </vaadin-button>
          <vaadin-button theme="secondary" ?disabled=${this.running} @click=${() => this.selectAll(true)}>${TEXT.selectAll}</vaadin-button>
          <vaadin-button theme="secondary" ?disabled=${this.running} @click=${() => this.selectAll(false)}>${TEXT.selectNone}</vaadin-button>
          <vaadin-button theme="secondary" ?disabled=${this.running} @click=${() => this.setAll(true)}>${TEXT.expandAll}</vaadin-button>
          <vaadin-button theme="secondary" ?disabled=${this.running} @click=${() => this.setAll(false)}>${TEXT.collapseAll}</vaadin-button>
        </div>
      </header>
      <div class="files">
        ${this.paths.map((path) => html`<ha-ops-preview-file
          data-testid="preview-file" .path=${path} .cursor=${this.cursor}
          .generation=${Number(this.state.operation_generation || 0)}
          .direction=${this.direction}
          .running=${this.running}
          .wrapLines=${this.isWrapped(path)}
          .selected=${this.isSelected(path)}
          .conflict=${this.isConflict(path)}
          .choice=${this.effectiveChoice(path)}
          @preview-select=${this.onPreviewSelect}
          @preview-resolve=${this.onPreviewResolve}
          @preview-wrap-toggle=${this.onPreviewWrapToggle}></ha-ops-preview-file>`)}
      </div>
      <footer>
        <div class=${`footer-actions ${this.direction === "save" ? "" : "apply-only"}`}>
          ${this.direction === "save" ? html`
            <label class="commit-subject-label" for="save-commit-subject">${TEXT.commitSubject || "Commit Subject:"}</label>
            <input
              id="save-commit-subject"
              class="commit-subject"
              name="commit_subject"
              .value=${this.commitSubject}
              ?disabled=${this.running}
              @input=${this.onCommitSubjectInput}>
          ` : nothing}
          <vaadin-button theme="primary" ?disabled=${this.isFinalActionDisabled()} @click=${() => this.runFinalAction()}>
            ${this.finalLabel}
          </vaadin-button>
        </div>
      </footer>
    `;
  }
  wrapAll(wrapLines) {
    const next = {};
    for (const path of this.paths) next[path] = Boolean(wrapLines);
    this.wrapByPath = next;
  }
  setAll(expanded) {
    if (this.running) return;
    for (const file of this.renderRoot.querySelectorAll("ha-ops-preview-file")) file.setExpanded(expanded);
  }
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
  onPreviewWrapToggle = (event) => {
    event.stopPropagation();
    this.wrapByPath = { ...this.wrapByPath, [event.detail.path]: Boolean(event.detail.wrapLines) };
  };
  onCommitSubjectInput = (event) => {
    this.commitSubject = event.target.value;
  };
  runFinalAction() {
    if (this.isFinalActionDisabled()) return;
    const payload = this.direction === "save"
      ? { commit_subject: this.commitSubject, default_commit_subject: this.defaultCommitSubject }
      : {};
    this.dispatchEvent(new CustomEvent("ha-ops-command", {
      bubbles: true,
      composed: true,
      detail: { command: this.finalCommand, payload },
    }));
  }
}
customElements.define("ha-ops-preview", HaOpsPreview);

function hasCommandInFlight(state, commands) {
  const runningStatuses = new Set(["accepted", "running", "failed_unknown"]);
  return Object.values(state.command_records || {})
    .some((record) => commands.includes(record.command) && runningStatuses.has(record.status));
}

const CLEANUP_RECOVERY_ACTIVE = new Set(["restore_required", "recovering", "manual_recovery"]);
const PENDING_FENCED_ACTIONS = new Set([
  "preview", "save_preview", "apply", "save", "reset_git_state", "disk_usage",
  "deleted_devices_preview", "deleted_devices_delete", "retained_devices_preview",
  "retained_devices_delete", "internal_ids_preview", "internal_ids_migrate",
  "docker_build_cache_prune",
]);
const PENDING_ALLOWED_ACTIONS = new Set(["deleted_devices_confirm", "deleted_devices_revert"]);

function deletedEntriesLabel(state, prefix = "last_deleted_devices") {
  const devices = Number(state[`${prefix}_device_count`] || 0);
  const entities = Number(state[`${prefix}_entity_count`] || 0);
  if (devices && entities) return TEXT.deletedDevicesAndEntitiesLabel;
  if (entities) return TEXT.deletedEntitiesLabel;
  return TEXT.deletedDevicesLabel;
}

function normalizePendingDeletedDevicesState(state) {
  const pending = Boolean(state.deleted_devices_pending_confirmation && state.deleted_devices_rollback_path);
  if (!pending) {
    state.deleted_devices_pending_diff = "";
    state.deleted_devices_pending_diff_error = "";
  }
  return state;
}

function deletedDevicesRecoveryActive(state) {
  return CLEANUP_RECOVERY_ACTIVE.has(state.deleted_devices_recovery_phase);
}

function renderDeletedDevicesTable(rows) {
  if (!rows?.length) return html`<p>${TEXT.noDeletedDevices}</p>`;
  const columnsByKey = {
    "area": ["area", TEXT.area, (row) => row.area || ""],
    "id": ["id", TEXT.id, (row) => row.id || ""],
    "entity-id": ["entity-id", TEXT.entityId, (row) => row.entity_id || ""],
    "name": ["name", TEXT.name, (row) => row.recovered_name || ""],
    "device": ["device", "Manufacturer and Model", (row) => {
      const model = [row.recovered_model, row.recovered_model_id].filter(Boolean).join(" / ");
      return [row.recovered_manufacturer, model].filter(Boolean).join("\n");
    }],
    "identifiers": ["identifiers", TEXT.identifiers, (row) => (row.recovered_identifiers || []).slice(0, 3).map((identifier) => Array.isArray(identifier) ? identifier.join(":") : String(identifier)).join(", ")],
    "original-name": ["original-name", TEXT.originalName, (row) => row.original_name || ""],
    "source": ["source", TEXT.source, (row) => [String(row.source_commit || "").slice(0, 12), row.source_path].filter(Boolean).join(" ")],
  };
  const primaryKeys = ["id", "original-name", "area", "device"];
  const secondaryKeys = ["identifiers", "name", "entity-id", "source"];
  const renderHeaderCells = (keys, line) => keys.map((key) => {
    const [_className, label] = columnsByKey[key];
    return html`<div class=${`deleted-device-header-cell deleted-device-cell-${line} deleted-device-col-${key}`}>${label}</div>`;
  });
  const renderRowCells = (keys, row, line) => keys.map((key) => {
        const [_className, _label, value] = columnsByKey[key];
        const text = String(value(row));
        return html`<div class=${`deleted-device-cell deleted-device-cell-${line} deleted-device-cell-${key} deleted-device-col-${key}`}>
          ${["id", "entity-id", "identifiers", "source"].includes(key) ? html`<code>${text}</code>` : text}
        </div>`;
      });
  return html`
    <div class="table-scroll">
      <div class="deleted-devices-table">
        <div class="deleted-device-header">
          ${renderHeaderCells(primaryKeys, "primary")}
          ${renderHeaderCells(secondaryKeys, "secondary")}
        </div>
        ${rows.map((row) => html`<div class="deleted-device-row">
          ${renderRowCells(primaryKeys, row, "primary")}
          ${renderRowCells(secondaryKeys, row, "secondary")}
        </div>`)}
      </div>
    </div>
  `;
}

function entityLabel(entity) {
  return entity?.entity_id || entity?.name || entity?.id || "";
}

function renderEntityList(label, entities) {
  if (!entities?.length) return nothing;
  return html`<p class="deleted-entity-label">${label}</p><ul>${entities.map((entity) => html`<li>${entityLabel(entity)}</li>`)}</ul>`;
}

function renderDeletedDevicesTree(tree) {
  if (!tree || typeof tree !== "object") return html`<p>${TEXT.noDeletedDevices}</p>`;
  const deviceGroups = tree.device_groups || [];
  const orphanGroups = tree.orphan_entity_groups || [];
  if (!deviceGroups.length && !orphanGroups.length) return html`<p>${TEXT.noDeletedDevices}</p>`;
  return html`
    <div class="deleted-devices-tree">
      ${(tree.warnings || []).map((warning) => html`<p class="action-hint">${warning}</p>`)}
      ${deviceGroups.map((group) => {
        const device = group.device || {};
        const counts = group.counts || {};
        const model = [device.manufacturer, device.model, device.model_id].filter(Boolean).join(" / ");
        const summary = [device.label || device.id || TEXT.deletedDevicesLabel, model, device.area].filter(Boolean).join(" · ");
        const meta = (TEXT.deletedDeviceGroupCounts || "{deleted} deleted, {active} active")
          .replace("{deleted}", String(Number(counts.deleted_entities || 0)))
          .replace("{active}", String(Number(counts.active_entities || 0)));
        const source = [String(device.source_commit || "").slice(0, 12), device.source_path].filter(Boolean).join(" ");
        const identifiers = (device.identifiers || []).slice(0, 3).map((identifier) =>
          Array.isArray(identifier) ? identifier.join(":") : String(identifier)
        ).join(", ");
        return html`
          <vaadin-details class="deleted-device-group" opened>
            <vaadin-details-summary slot="summary">
              <span class="deleted-device-summary-main">${summary}</span>
              <span class="deleted-device-summary-meta">${meta}</span>
            </vaadin-details-summary>
            ${source || identifiers ? html`<p><small>${[source, identifiers].filter(Boolean).join(" · ")}</small></p>` : nothing}
            ${renderEntityList(TEXT.deletedEntitiesLabel, group.deleted_entities || [])}
            ${renderEntityList(TEXT.activeEntitiesLabel || "Active entities", group.active_entities || [])}
          </vaadin-details>
        `;
      })}
      ${orphanGroups.map((group) => html`
        <vaadin-details class="deleted-device-group orphan-entities" opened>
          <vaadin-details-summary slot="summary">
            <span class="deleted-device-summary-main">${group.label || TEXT.deletedEntitiesLabel}</span>
          </vaadin-details-summary>
          ${renderEntityList(TEXT.deletedEntitiesLabel, group.deleted_entities || [])}
        </vaadin-details>
      `)}
    </div>
  `;
}

class HaOpsPendingRawDiff extends LitElement {
  static properties = { opened: { type: Boolean }, diff: { type: String }, diffState: { type: String } };
  static styles = css`
    :host { display: block; min-width: 0; max-width: 100%; margin-top: .85rem; }
    vaadin-details { border: 1px solid var(--ha-ops-border, #d0d7de); border-radius: 8px; overflow: hidden; min-width: 0; max-width: 100%; }
    vaadin-details::part(content) { min-width: 0; max-width: 100%; overflow: hidden; }
    vaadin-details-summary { width: 100%; }
    pre { box-sizing: border-box; width: 100%; max-width: 100%; min-width: 0; margin: 0; padding: .75rem; overflow-x: auto; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere; border-top: 1px solid var(--ha-ops-border, #d0d7de); background: var(--ha-ops-code-bg, #f6f8fa); }
    .line { display: block; min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ha-ops-code-text, #24292f); }
    .diff-add { color: var(--ha-ops-diff-add-text, #116329); background: var(--ha-ops-diff-add-bg, #dafbe1); }
    .diff-del { color: var(--ha-ops-diff-del-text, #82071e); background: var(--ha-ops-diff-del-bg, #ffebe9); }
    .diff-hunk { color: var(--ha-ops-diff-hunk-text, #0550ae); background: var(--ha-ops-diff-hunk-bg, #ddf4ff); }
    .diff-file, .diff-context { background: transparent; }
    [role="status"] { padding: .75rem; color: var(--ha-ops-muted-text, #57606a); }
  `;
  constructor() {
    super();
    this.opened = false;
    this.diff = "";
    this.diffState = "idle";
  }
  render() {
    return html`
      <vaadin-details .opened=${this.opened} @opened-changed=${this.onOpenedChanged}>
        <vaadin-details-summary slot="summary">${TEXT.advancedRawDiff || "Advanced raw diff"}</vaadin-details-summary>
        ${this.diffState === "loaded"
          ? html`<pre aria-label=${TEXT.conflictDiffTitle || "Conflict diff"}>${highlightedDiffLines(this.diff)}</pre>`
          : html`<div role="status">${this.diffState === "error" ? this.diff : TEXT.rawDiffLoadsOnExpand || "Raw registry diff loads only when this section is expanded."}</div>`}
      </vaadin-details>
    `;
  }
  async onOpenedChanged(event) {
    const opened = Boolean(event.detail.value);
    this.opened = opened;
    if (!opened || this.diffState === "loaded" || this.diffState === "loading") return;
    this.diffState = "loading";
    try {
      const response = await fetch("pending-deleted-devices-diff-get");
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.message || "Raw diff unavailable");
      this.diff = payload.diff || "";
      this.diffState = "loaded";
    } catch (error) {
      this.diff = error.message;
      this.diffState = "error";
    }
  }
}
customElements.define("ha-ops-pending-raw-diff", HaOpsPendingRawDiff);

function renderRetainedDevicesTable(rows, disabled) {
  if (!rows?.length) return html`<p>${TEXT.noRetainedDevices}</p>`;
  return html`
    <div class="table-scroll">
      <table class="retained-devices-table">
        <colgroup><col class="checkbox-col"><col><col><col><col></colgroup>
        <thead><tr>
          <th class="checkbox-col" aria-label=${TEXT.deleteLabel}></th>
          <th>${TEXT.identifiers}</th>
          <th>${TEXT.name}</th>
          <th>${TEXT.manufacturerModel}</th>
          <th>${TEXT.retainedDiscoveryTopics}</th>
        </tr></thead>
        <tbody>
          ${rows.map((row) => html`<tr>
            <td class="checkbox-col">
              <input type="checkbox" name="candidate" value=${row.identity || ""} ?checked=${row.selected !== false} ?disabled=${disabled}>
            </td>
            <td><code>${String(row.identifiers || "")}</code></td>
            <td>${row.name || ""}</td>
            <td>${[row.manufacturer, row.model].filter(Boolean).join(" | ")}</td>
            <td><pre>${(row.retained_topics || []).join("\n")}</pre></td>
          </tr>`)}
        </tbody>
      </table>
    </div>
  `;
}

class HaOpsApp extends LitElement {
  static properties = {
    connection: { type: String },
    revision: { type: Number },
    state: { type: Object },
    confirmOpen: { type: Boolean },
    confirmMessage: { type: String },
    clientVersion: { type: String },
    backendVersion: { type: String },
    versionMismatchOpen: { type: Boolean },
  };

  static styles = css`
    :host { display: contents; }
    vaadin-confirm-dialog.version-mismatch {
      --vaadin-confirm-dialog-width: min(420px, calc(100vw - 32px));
      --vaadin-confirm-dialog-max-width: calc(100vw - 32px);
    }
    vaadin-confirm-dialog.version-mismatch::part(backdrop) {
      background: rgba(0, 0, 0, 0.33);
    }
    vaadin-confirm-dialog.version-mismatch vaadin-button.version-mismatch-ack {
      --vaadin-button-background: #f6f8fa;
      --vaadin-button-border-color: #8c959f;
      --vaadin-button-border-radius: 6px;
      --vaadin-button-border-width: 1px;
      --vaadin-button-text-color: #24292f;
      font-weight: 700;
      margin-inline-end: 8px;
    }
    vaadin-confirm-dialog.version-mismatch vaadin-button.version-mismatch-ack:hover {
      --vaadin-button-background: #eaeef2;
      --vaadin-button-border-color: #6e7781;
    }
    vaadin-confirm-dialog.version-mismatch vaadin-button.version-mismatch-ack:focus-visible {
      outline: 2px solid #0969da;
      outline-offset: 2px;
    }
  `;

  constructor() {
    super();
    this.connection = "connecting";
    this.revision = 0;
    this.state = {};
    this.confirmOpen = false;
    this.confirmMessage = "";
    this.confirmForm = null;
    this.clientVersion = knownVersion(window.__HA_OPS_BOOT_VERSION__) ? String(window.__HA_OPS_BOOT_VERSION__) : null;
    this.backendVersion = this.clientVersion;
    this.acknowledgedBackendVersion = null;
    this.versionMismatchOpen = false;
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
      <vaadin-confirm-dialog
        class="version-mismatch"
        .opened=${this.versionMismatchOpen}
        .header=${TEXT.versionMismatchTitle || "New HA Ops Version Available"}
        .message=${this.versionMismatchMessage()}
        .confirmText=${TEXT.reloadHaOps || "Reload HA Ops"}
        reject-button-visible
        @confirm=${this.reloadHaOps}
        @cancel=${this.acknowledgeVersionMismatch}
      >
        <vaadin-button
          slot="reject-button"
          class="version-mismatch-ack"
          theme="secondary"
          @click=${this.acknowledgeVersionMismatch}
        >
          ${TEXT.acknowledgeRisksContinue || "Acknowledge Risks & Continue"}
        </vaadin-button>
      </vaadin-confirm-dialog>
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
    this.observeBackendVersion(frame.backend_version);
    this.state = normalizePendingDeletedDevicesState(structuredClone(frame.state));
    this.revision = Number(frame.revision ?? frame.state_revision ?? frame.state.state_revision ?? 0);
    this.syncDom();
  }

  applyPatch(frame) {
    this.observeBackendVersion(frame.backend_version);
    const base = Number(frame.base_revision);
    const revision = Number(frame.revision);
    if (revision <= this.revision) return;
    if (base !== this.revision) {
      this.replayPending = true;
      this.setConnection("replaying");
      this.socket?.send(JSON.stringify({ id: String(this.nextRequestId++), command: "replay" }));
      return;
    }
    this.state = normalizePendingDeletedDevicesState({ ...this.state, ...(frame.patch || {}) });
    this.revision = revision;
    this.syncDom();
  }

  observeBackendVersion(version) {
    if (!knownVersion(version) || !knownVersion(this.clientVersion)) {
      this.backendVersion = knownVersion(version) ? String(version) : this.backendVersion;
      this.versionMismatchOpen = false;
      return;
    }
    const backendVersion = String(version);
    this.backendVersion = backendVersion;
    this.versionMismatchOpen = backendVersion !== this.clientVersion
      && this.acknowledgedBackendVersion !== backendVersion;
  }

  versionMismatchMessage() {
    const version = this.backendVersion || "";
    const template = TEXT.versionMismatchWarning
      || "A new HA Ops version {version} is available. Correct client operation is not guaranteed until you reload HA Ops.";
    return template.replaceAll("{version}", version);
  }

  reloadHaOps = () => {
    window.location.reload();
  };

  acknowledgeVersionMismatch = () => {
    if (knownVersion(this.backendVersion)) this.acknowledgedBackendVersion = String(this.backendVersion);
    this.versionMismatchOpen = false;
  };

  syncDom() {
    const running = this.state.last_status === "running" || Object.values(this.state.command_records || {})
      .some((record) => ["accepted", "running", "failed_unknown"].includes(record.status));
    const pending = Boolean(this.state.deleted_devices_pending_confirmation);
    const recovery = deletedDevicesRecoveryActive(this.state);
    const saveRetry = Boolean(this.state.save_push_retry_pending);
    const dockerFenceActive = Boolean(this.state.docker_build_cache_prune_fence);
    for (const control of this.querySelectorAll("vaadin-button, vaadin-checkbox, vaadin-details, vaadin-select")) {
      if (control.matches("[data-read-only-control]")) continue;
      const form = control.closest("form");
      const action = form ? commandForAction(form.action) : "";
      if (action === "docker_build_cache_prune") {
        const capabilityAvailable = form?.dataset.capabilityAvailable === "true";
        const ready = capabilityAvailable && !running && !saveRetry && !pending && !recovery && !dockerFenceActive;
        if (form) form.dataset.actionReady = ready ? "true" : "false";
        control.disabled = !ready;
      } else if (PENDING_FENCED_ACTIONS.has(action) || PENDING_ALLOWED_ACTIONS.has(action)) {
        control.disabled = recovery
          ? action !== "deleted_devices_revert"
          : running || saveRetry || (pending && !PENDING_ALLOWED_ACTIONS.has(action));
      } else if (recovery) {
        control.disabled = action ? action !== "deleted_devices_revert" : running || control.hasAttribute("data-server-disabled");
      } else {
        control.disabled = running || control.hasAttribute("data-server-disabled");
      }
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
    const cleanupRunning = hasCommandInFlight(this.state, ["deleted_devices_preview", "retained_devices_preview", "deleted_devices_delete", "retained_devices_delete", "internal_ids_preview", "internal_ids_migrate"]);
    const pendingDeletedCleanup = Boolean(this.state.deleted_devices_pending_confirmation);
    const hasDeletedPreview = Boolean(this.state.last_deleted_devices_generated_at);
    const hasRetainedPreview = Boolean(this.state.last_retained_devices_generated_at);
    const visible = hasApplyPaths || hasSavePaths || previewRunning || hasDeletedPreview || hasRetainedPreview || cleanupRunning || pendingDeletedCleanup;
    for (const element of this.querySelectorAll("[data-server-cleanup-preview]")) element.hidden = Boolean(hasDeletedPreview || hasRetainedPreview || cleanupRunning || pendingDeletedCleanup);
    if (!visible) {
      render(nothing, host);
      return;
    }
    const loading = previewRunning && !hasApplyPaths && !hasSavePaths;
    render(html`
      ${this.renderDeletedPreview(cleanupRunning)}
      ${this.renderRetainedPreview(cleanupRunning)}
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

  renderDeletedPreview(cleanupRunning) {
    if (this.state.deleted_devices_pending_confirmation) {
      const entries = deletedEntriesLabel(this.state, "deleted_devices_pending");
      const pendingCount = Number(this.state.deleted_devices_pending_device_count || 0) + Number(this.state.deleted_devices_pending_entity_count || 0);
      const title = (TEXT.pendingDeletedDevicesTitle || "Pending {entries} cleanup").replace("{entries}", entries);
      const removedText = (TEXT.pendingDeletedDevicesRemoved || "- {entries} removed by this cleanup: {count}")
        .replace("{entries}", entries)
        .replace("{count}", String(pendingCount))
        .replace(/^\s*-\s*/, "");
      const pendingTree = this.state.deleted_devices_pending_tree;
      const pendingTreeError = this.state.deleted_devices_pending_tree_error || "";
      const unavailableTemplate = TEXT.pendingDiffUnavailable || "Pending diff unavailable: {error}";
      return html`
        <section class="card wide" data-testid="deleted-devices-preview-section">
          <h2>${title}</h2>
          <p>${this.state.last_message || TEXT.pendingDeletedDevicesMessage || "Deleted devices cleanup is waiting for your decision."}</p>
          <p>${removedText}</p>
          <p>${TEXT.deletedDevicesPendingNotice || "Confirm Changes keeps this cleanup. Revert Changes restores only entries removed by this cleanup."}</p>
          ${pendingTree ? renderDeletedDevicesTree(pendingTree) : html`<p>${unavailableTemplate.replace("{error}", pendingTreeError)}</p>`}
          <ha-ops-pending-raw-diff></ha-ops-pending-raw-diff>
          <div class="actions deletion-actions"><div class="action-row">
            <form method="post" action="deleted-devices-confirm" data-async-form="true" data-preserve-display-state="true">
              <button type="submit" ?disabled=${this.isRunning()}>${TEXT.confirmChanges || "Confirm Changes"}</button>
            </form>
            <form method="post" action="deleted-devices-revert" data-async-form="true" data-preserve-display-state="true">
              <button type="submit" class="secondary" ?disabled=${this.isRunning()}>${TEXT.revertDeletedDevices || "Revert Changes"}</button>
            </form>
          </div></div>
        </section>
      `;
    }
    const rows = this.state.last_deleted_devices_rows || [];
    const tree = this.state.last_deleted_devices_tree;
    const count = Number(this.state.last_deleted_devices_count || 0);
    const visible = Boolean(this.state.last_deleted_devices_generated_at) || cleanupRunning && this.state.last_action === "deleted_devices_preview";
    if (!visible) return nothing;
    const disabled = this.isRunning() || Boolean(this.state.deleted_devices_pending_confirmation) || !count || !this.state.last_deleted_devices_fingerprint;
    const entries = deletedEntriesLabel(this.state);
    const confirmMessage = TEXT.confirmDeletedDevicesDelete.replace("{entries}", entries);
    return html`
      <section class="card wide" data-testid="deleted-devices-preview-section">
        <h2>${TEXT.deletedDevicesPreview}</h2>
        <p>${TEXT.generatedAt} <span data-transient="deleted-devices-generated">${this.state.last_deleted_devices_generated_at || ""}</span></p>
        <div data-transient="deleted-devices-preview">${tree ? renderDeletedDevicesTree(tree) : renderDeletedDevicesTable(rows)}</div>
        ${count > 0 ? html`
          <div class="actions deletion-actions"><div class="action-row">
            <form method="post" action="deleted-devices-delete" data-async-form="true" data-preserve-display-state="true" data-confirm=${confirmMessage}>
              <button type="submit" ?disabled=${disabled}>${TEXT.approveDeletedDevices}</button>
            </form>
          </div></div>
        ` : nothing}
      </section>
    `;
  }

  renderRetainedPreview(cleanupRunning) {
    const rows = this.state.last_retained_devices_rows || [];
    const visible = Boolean(this.state.last_retained_devices_generated_at) || cleanupRunning && this.state.last_action === "retained_devices_preview";
    if (!visible) return nothing;
    const disabled = this.isRunning() || Boolean(this.state.deleted_devices_pending_confirmation) || !rows.length || !this.state.last_retained_devices_fingerprint;
    return html`
      <section class="card wide" data-testid="retained-devices-preview-section">
        <h2>${TEXT.retainedDevicesPreview}</h2>
        <p class="muted">${TEXT.retainedPreviewNotice}</p>
        <p class="muted">${TEXT.retainedDeleteNotice}</p>
        <p>${TEXT.generatedAt} <span data-transient="retained-devices-generated">${this.state.last_retained_devices_generated_at || ""}</span></p>
        <form method="post" action="retained-devices-delete" data-async-form="true" data-preserve-display-state="true" data-confirm=${TEXT.confirmRetainedDevicesDelete}>
          <input type="hidden" name="retained_preview_fingerprint" value=${this.state.last_retained_devices_fingerprint || ""}>
          <input type="hidden" name="retained_preview_generated_at" value=${this.state.last_retained_devices_generated_at || ""}>
          <div data-transient="retained-devices-preview">${renderRetainedDevicesTable(rows, disabled)}</div>
          ${rows.length ? html`<div class="actions deletion-actions"><div class="action-row">
            <button type="submit" ?disabled=${disabled}>${TEXT.deleteRetainedDevices}</button>
          </div></div>` : nothing}
        </form>
      </section>
    `;
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
    const status = this.state.deleted_devices_pending_confirmation ? "pending decision" : this.state.last_status || "idle";
    badge.dataset.connectionState = this.connection;
    if (this.connection === "unknown" || (status === "idle" && this.isDegradedConnection())) {
      badge.dataset.statusCode = "transport";
      badge.textContent = this.connection;
      badge.className = "badge transport";
      return;
    }
    badge.dataset.statusCode = status;
    badge.textContent = status === "success" ? TEXT.statusDone || "done" : status === "pending decision" ? TEXT.statusPendingDecision || "pending decision" : status;
    badge.className = `badge ${status === "success" ? "" : status === "pending decision" ? "pending" : status}`.trim();
  }
}
customElements.define("ha-ops-app", HaOpsApp);
