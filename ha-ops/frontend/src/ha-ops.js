import { LitElement, css, html, nothing } from "lit";
import "@vaadin/button";
import "@vaadin/checkbox";
import "@vaadin/confirm-dialog";
import "@vaadin/details";
import "@vaadin/progress-bar";
import "@vaadin/select";
import "@vaadin/text-field";

let TEXT = {};
const TEXT_KEYS = {
  "expand": "button.expand_diff",
  "collapse": "button.collapse_diff",
  "expandAll": "button.expand_all",
  "collapseAll": "button.collapse_all",
  "selectAll": "button.select_all",
  "selectNone": "button.select_none",
  "wrapLines": "button.wrap_lines",
  "unwrapLines": "button.unwrap_lines",
  "wrapAllLines": "button.wrap_all_lines",
  "unwrapAllLines": "button.unwrap_all_lines",
  "changeList": "heading.change_list",
  "deletedDevicesPreview": "heading.deleted_devices_preview",
  "retainedDevicesPreview": "heading.retained_devices_preview",
  "gitAccess": "heading.git_access",
  "applyPreview": "heading.git_to_ha",
  "savePreview": "heading.ha_to_git",
  "apply": "action.apply",
  "deleteRetainedDevices": "action.delete_retained_devices",
  "removeDeletedEntries": "action.remove_deleted_entries",
  "revertDeletedDevices": "action.revert_changes",
  "save": "action.save",
  "useGitVersion": "action.use_git_version",
  "useHaVersion": "action.use_ha_version",
  "confirmDeletedDevicesDelete": "confirm.deleted_devices_delete",
  "confirmRetainedDevicesDelete": "confirm.retained_devices_delete",
  "reloadHaOps": "action.reload_ha_ops",
  "acknowledgeRisksContinue": "action.acknowledge_risks_continue",
  "versionMismatchTitle": "heading.version_mismatch",
  "versionMismatchWarning": "warning.version_mismatch",
  "includeFile": "label.include_preview_file",
  "area": "label.area",
  "id": "label.id",
  "entityId": "label.entity_id",
  "generatedAt": "label.generated_at",
  "identifiers": "label.identifiers",
  "name": "label.name",
  "manufacturerModel": "label.manufacturer_model",
  "originalName": "label.original_name",
  "originalDeviceClass": "label.original_device_class",
  "retainedDiscoveryTopics": "label.retained_discovery_topics",
  "source": "label.source",
  "deleteLabel": "label.delete",
  "commitSubject": "label.commit_subject",
  "versionChoice": "label.preview_version_choice",
  "loadingDiff": "message.loading_diff",
  "loadingPreviewDiff": "message.loading_preview_diff",
  "unavailableDiff": "text.diff_detail_unavailable",
  "noDeletedDevices": "text.no_deleted_devices",
  "noRetainedDevices": "text.no_retained_devices",
  "retainedPreviewNotice": "notice.retained_devices_preview",
  "retainedDeleteNotice": "notice.retained_devices_delete",
  "deletedDevicesLabel": "label.deleted_devices",
  "deletedEntitiesLabel": "label.deleted_entities",
  "deletedDevicesAndEntitiesLabel": "label.deleted_devices_and_entities",
  "activeEntitiesLabel": "label.active_entities",
  "entitiesToRemoveLabel": "label.entities_to_remove",
  "deletedDevicesPendingNotice": "notice.deleted_devices_pending",
  "pendingDeletedDevicesMessage": "message.pending_deleted_devices",
  "pendingDeletedDevicesRemoved": "text.cleanup_removed",
  "pendingDeletedDevicesTitle": "heading.pending_deleted_devices_diff",
  "pendingDiffUnavailable": "error.pending_diff_unavailable",
  "advancedRawDiff": "heading.advanced_raw_diff",
  "registryChanges": "heading.registry_changes",
  "registryAdded": "text.registry_added",
  "registryRemoved": "text.registry_removed",
  "registryChanged": "text.registry_changed",
  "registryFields": "text.registry_fields",
  "registryReview": "text.registry_review",
  "rawDiffLoadsOnExpand": "text.raw_diff_loads_on_expand",
  "deletedDeviceGroupActiveCount": "text.deleted_device_group_active_count",
  "deletedDeviceGroupRemoveCount": "text.deleted_device_group_remove_count",
  "deletedDevicePreviousZigbee2mqttApp": "text.deleted_device_previous_zigbee2mqtt_app",
  "conflictDiffTitle": "title.conflict_diff",
  "statusDone": "status.done",
  "statusPendingDecision": "status.pending_decision",
  "confirm": "action.confirm",
  "confirmChanges": "action.confirm_changes"
};
const t = (key, values = {}) => {
  let result = TEXT.catalog?.[key] || key;
  for (const [name, value] of Object.entries(values)) result = result.replaceAll(`{${name}}`, String(value));
  return result;
};
const WS_COMMANDS = new Set([
  "preview", "save_preview", "apply", "save", "select_save_preview", "select_apply_preview",
  "resolve_save_preview", "resolve_apply_preview", "reset_git_state", "disk_usage",
  "deleted_devices_preview", "retained_devices_preview", "retained_devices_delete",
  "select_retained_device",
  "internal_ids_preview", "internal_ids_migrate", "select_internal_ids", "deleted_devices_delete",
  "acknowledge_recovery",
  "retry_interrupted_save",
  "deleted_devices_confirm", "deleted_devices_revert", "rollback",
]);
const TERMINAL_STATE_SYNC_COMMANDS = new Set(["deleted_devices_confirm", "deleted_devices_revert"]);

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
      preview_id: state.save_preview_id ?? null,
      decision_revision: Number(state.save_decision_revision || 0),
      commit: state.last_save_preview_commit ?? null,
      fingerprint: state.last_save_preview_fingerprint ?? null,
      paths: sortedStrings(state.last_save_preview_paths),
      conflict_paths: sortedStrings(state.last_save_preview_conflict_paths),
      diff_cursor: cursorIdentity(state.last_save_diff_cursor),
    };
  }
  return {
    direction: "apply",
    preview_id: state.apply_preview_id ?? null,
    decision_revision: Number(state.apply_decision_revision || 0),
    commit: state.last_preview_commit ?? null,
    fingerprint: state.last_preview_fingerprint ?? null,
    live_fingerprints: sortedObject(state.last_preview_live_fingerprints),
    paths: sortedStrings(state.last_preview_paths),
    conflict_paths: sortedStrings(state.last_preview_conflict_paths),
    diff_cursor: cursorIdentity(state.last_diff_cursor),
  };
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
    expanded: { type: Boolean }, diff: { type: String }, semantic: { type: Object }, diffState: { type: String },
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
    .registry-summary { padding: .75rem; border-top: 1px solid var(--ha-ops-border, #d0d7de); }
    .registry-summary h4 { margin: 0 0 .4rem; }
    .registry-summary p { margin: 0 0 .5rem; }
    .registry-summary ul { margin: 0; padding-left: 1.4rem; }
    .registry-summary li { margin: .25rem 0; overflow-wrap: anywhere; }
    .registry-summary .review { color: #82071e; font-weight: 600; margin-left: .35rem; }
    .registry-summary small { display: block; color: var(--ha-ops-muted-text, #57606a); }
    .raw-registry-diff { margin-top: .75rem; }
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
    this.semantic = null;
    this.diffState = "idle";
    this.selected = false;
    this.choice = "";
    this.conflict = false;
    this.direction = "apply";
    this.running = false;
    this.wrapLines = true;
    this.diffRequestId = 0;
  }
  willUpdate(changed) {
    const cursorChanged = changed.has("cursor") && cursorKey(changed.get("cursor")) !== cursorKey(this.cursor);
    const pathChanged = changed.has("path") && changed.get("path") !== this.path;
    if (cursorChanged || changed.has("generation") || pathChanged) {
      this.diffRequestId += 1;
      this.expanded = false; this.diff = ""; this.semantic = null; this.diffState = "idle";
    }
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
          ? this.semantic ? html`
              <section class="registry-summary" aria-label=${TEXT.registryChanges || "Registry changes"}>
                <h4>${TEXT.registryChanges || "Registry changes"}</h4>
                <p>${TEXT.registryAdded || "Added"}: ${this.semantic.counts.added || 0} · ${TEXT.registryRemoved || "Removed"}: ${this.semantic.counts.removed || 0} · ${TEXT.registryChanged || "Changed"}: ${this.semantic.counts.changed || 0}</p>
                <ul>${this.semantic.rows.map(row => html`<li>
                  ${row.kind === "added" ? TEXT.registryAdded : row.kind === "removed" ? TEXT.registryRemoved : TEXT.registryChanged}: <code>${row.old_label ? `${row.old_label} → ${row.label}` : row.label}</code>
                  ${row.review ? html`<span class="review">${TEXT.registryReview || "Review"}</span>` : nothing}
                  ${row.fields.length ? html`<small>${(TEXT.registryFields || "Fields: {fields}").replace("{fields}", row.fields.join(", "))}</small>` : nothing}
                </li>`)}</ul>
                <vaadin-details class="raw-registry-diff">
                  <vaadin-details-summary slot="summary">${TEXT.advancedRawDiff || "Advanced raw diff"}</vaadin-details-summary>
                  <pre class=${this.wrapLines ? "wrap-lines" : ""} aria-label="Diff detail">${highlightedDiffLines(this.diff)}</pre>
                </vaadin-details>
              </section>`
            : html`<pre class=${this.wrapLines ? "wrap-lines" : ""} aria-label="Diff detail">${highlightedDiffLines(this.diff)}</pre>`
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
    const requestId = ++this.diffRequestId;
    this.expanded = expanded;
    if (!expanded || this.diffState === "loaded") return;
    const cursor = JSON.stringify(this.cursor);
    const path = this.path;
    const generation = this.generation;
    this.diffState = "loading";
    try {
      const response = await fetch(`diff-get?cursor=${encodeURIComponent(cursor)}&path=${encodeURIComponent(path)}`);
      const payload = await response.json();
      if (requestId !== this.diffRequestId || !this.expanded || cursor !== JSON.stringify(this.cursor) || path !== this.path
        || generation !== this.generation) return;
      if (!payload.ok || Number(this.cursor?.generation) !== Number(this.generation)) throw new Error("stale");
      this.diff = payload.diff;
      this.semantic = payload.semantic || null;
      this.diffState = "loaded";
    } catch (_error) {
      if (requestId !== this.diffRequestId || !this.expanded || cursor !== JSON.stringify(this.cursor) || path !== this.path
        || generation !== this.generation) return;
      this.diff = "";
      this.semantic = null;
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
    const requested = event.target.checked;
    event.target.checked = this.selected;
    this.dispatchEvent(new CustomEvent("preview-select", {
      bubbles: true,
      composed: true,
      detail: { path: this.path, selected: requested },
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
    generatedAt: { type: String },
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
    .footer-actions { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: .5rem; min-width: 0; max-width: 100%; width: 100%; }
    .footer-actions.apply-only { display: flex; justify-content: flex-end; }
    vaadin-text-field.commit-subject { box-sizing: border-box; width: 100%; min-width: 0; max-width: 100%; }
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
    const identityKey = this.direction === "save" ? this.state.save_preview_id : this.state.apply_preview_id;
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
        <div><h3>${this.direction === "save" ? TEXT.savePreview : TEXT.applyPreview}</h3>
          ${this.generatedAt ? html`<small>${TEXT.generatedAt} ${this.generatedAt}</small>` : nothing}</div>
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
            <vaadin-text-field
              id="save-commit-subject"
              class="commit-subject"
              .label=${TEXT.commitSubject || "Commit Subject:"}
              .value=${this.commitSubject}
              ?disabled=${this.running}
              @input=${this.onCommitSubjectInput}></vaadin-text-field>
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
  async runFinalAction() {
    if (this.isFinalActionDisabled()) return;
    const selected = new Set(this.selectedPaths);
    const decisions = [...this.paths].sort().map((path) => ({
      choice: selected.has(path) ? (this.resolutions[path] || (this.direction === "save" ? "ha" : "git"))
        : (this.direction === "save" ? "git" : "ha"),
      path,
      selected: selected.has(path),
    }));
    const bytes = new TextEncoder().encode(JSON.stringify(decisions));
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
      .map((byte) => byte.toString(16).padStart(2, "0")).join("");
    const payload = this.direction === "save"
      ? { commit_subject: this.commitSubject, default_commit_subject: this.defaultCommitSubject }
      : {};
    payload.preview_identity = previewIdentity(this.state, this.direction);
    payload.decision_digest = digest;
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
        const deletedCount = Number(counts.deleted_entities || 0);
        const activeCount = Number(counts.active_entities || 0);
        const metaParts = [
          (TEXT.deletedDeviceGroupRemoveCount || "{deleted} entities to remove").replace("{deleted}", String(deletedCount)),
        ];
        if (activeCount > 0) {
          metaParts.push((TEXT.deletedDeviceGroupActiveCount || "{active} active entities").replace("{active}", String(activeCount)));
        }
        const meta = metaParts.join(", ");
        const source = [String(device.source_commit || "").slice(0, 12), device.source_path].filter(Boolean).join(" ");
        const identifiers = (device.identifiers || []).slice(0, 3).map((identifier) =>
          Array.isArray(identifier) ? identifier.join(":") : String(identifier)
        ).join(", ");
        const reason = group.presentation_reason || {};
        return html`
          <vaadin-details class="deleted-device-group" opened>
            <vaadin-details-summary slot="summary">
              <div class="deleted-device-summary-row">
                <span class="deleted-device-summary-left">
                  <span class="deleted-device-summary-main">${summary}</span>
                  <span class="deleted-device-summary-meta">${meta}</span>
                </span>
                ${identifiers ? html`<code class="deleted-device-summary-identifier">${identifiers}</code>` : nothing}
              </div>
            </vaadin-details-summary>
            ${source ? html`<p><small>${source}</small></p>` : nothing}
            ${reason.kind === "previous_zigbee2mqtt_app" ? html`<p class="action-hint">${(TEXT.deletedDevicePreviousZigbee2mqttApp || "Historical presentation only: these entities are associated with previous Zigbee2MQTT App/Supervisor slug {old_slug}; current App slug is {current_slug}.").replace("{old_slug}", reason.old_slug || "").replace("{current_slug}", reason.current_slug || "")}</p>` : nothing}
            ${renderEntityList(TEXT.entitiesToRemoveLabel || "Entities to remove", group.deleted_entities || [])}
            ${renderEntityList(TEXT.activeEntitiesLabel || "Active entities", group.active_entities || [])}
          </vaadin-details>
        `;
      })}
      ${orphanGroups.map((group) => html`
        <vaadin-details class="deleted-device-group orphan-entities" opened>
          <vaadin-details-summary slot="summary">
            <span class="deleted-device-summary-main">${group.label || TEXT.deletedEntitiesLabel}</span>
          </vaadin-details-summary>
          ${renderEntityList(TEXT.entitiesToRemoveLabel || "Entities to remove", group.deleted_entities || [])}
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

function renderRetainedDevicesTable(rows, disabled, onToggle) {
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
              <vaadin-checkbox aria-label=${`${TEXT.deleteLabel} ${row.name || row.identity || ""}`}
                .checked=${Boolean(row.selected)} ?disabled=${disabled}
                @change=${(event) => { const selected = event.target.checked; event.target.checked = Boolean(row.selected);
                  onToggle(row.identity, selected); }}></vaadin-checkbox>
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
    view: { type: Object },
    confirmOpen: { type: Boolean },
    confirmMessage: { type: String },
    clientVersion: { type: String },
    backendVersion: { type: String },
    versionMismatchOpen: { type: Boolean },
    acceptedCommandId: { type: String },
    uncertainCommandId: { type: String },
    clientError: { type: String },
    managedTargetsOpen: { type: Boolean },
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
    .deleted-device-summary-row {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
      width: 100%;
    }
    vaadin-details-summary::part(content) {
      width: 100%;
      min-width: 0;
    }
    .deleted-device-summary-left {
      min-width: 0;
      flex: 1 1 auto;
    }
    .deleted-device-summary-main {
      display: block;
      font-size: 1rem;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .deleted-device-summary-meta {
      display: block;
      color: var(--ha-ops-muted-text, #57606a);
      font-size: .9rem;
      font-weight: 400;
      margin-top: 2px;
      overflow-wrap: anywhere;
    }
    .deleted-device-summary-identifier {
      flex: 0 1 auto;
      max-width: 48%;
      color: var(--ha-ops-muted-text, #57606a);
      font-size: .85rem;
      font-weight: 400;
      overflow-wrap: anywhere;
      text-align: right;
      white-space: normal;
    }
    .deleted-device-group li {
      min-width: 0;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    @media (max-width: 640px) {
      .deleted-device-summary-row {
        flex-wrap: wrap;
      }
      .deleted-device-summary-identifier {
        flex-basis: 100%;
        max-width: 100%;
        text-align: left;
      }
    }
  `;

  constructor() {
    super();
    this.connection = "connecting";
    this.revision = 0;
    this.state = {};
    this.view = {};
    this.confirmOpen = false;
    this.confirmMessage = "";
    this.clientVersion = knownVersion(window.__HA_OPS_BOOT_VERSION__) ? String(window.__HA_OPS_BOOT_VERSION__) : null;
    this.backendVersion = this.clientVersion;
    this.acknowledgedBackendVersion = null;
    this.versionMismatchOpen = false;
    this.socket = null;
    this.pending = new Map();
    this.nextRequestId = 1;
    this.reconnectTimer = null;
    this.httpPollTimer = null;
    this.reconnectStableTimer = null;
    this.reconnectDelayMs = 1200;
    this.replayPending = true;
    this.queuedFrames = [];
    this.internalDiffs = new Map();
    this.conflictDiffs = new Map();
    this.shouldReconnect = false;
    this.acceptedCommandId = null;
    this.uncertainCommandId = null;
    this.clientError = "";
    this.managedTargetsOpen = false;
  }

  connectedCallback() {
    super.connectedCallback();
    this.shouldReconnect = true;
    this.connect();
    if (window.__HA_OPS_ENABLE_TEST_HOOKS__ === true) window.__haOpsTestCloseWs = () => this.socket?.close();
  }

  disconnectedCallback() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.httpPollTimer) clearTimeout(this.httpPollTimer);
    if (this.reconnectStableTimer) clearTimeout(this.reconnectStableTimer);
    this.shouldReconnect = false;
    if (this.socket) this.socket.close();
    super.disconnectedCallback();
  }

  createRenderRoot() { return this; }

  updated() {
    if (!this.resizeObserver) this.observeLayout();
  }

  actionButton(command, label, { disabled = false, confirm = "", payload = {}, theme = "secondary" } = {}) {
    return html`<vaadin-button theme=${theme} ?disabled=${disabled}
      @click=${() => this.issue(command, payload, confirm)}>${label}</vaadin-button>`;
  }

  issue(command, payload = {}, confirmation = "") {
    if (confirmation) {
      this.confirmCommand = { command, payload };
      this.confirmMessage = confirmation;
      this.confirmOpen = true;
      return;
    }
    const action = new URL(command.replaceAll("_", "-"), baseUrl()).href;
    this.dispatchCommand(command, action, payload).catch((error) => this.handleCommandError(error));
  }

  mutationBlocked() {
    return Boolean(this.acceptedCommandId || this.uncertainCommandId) || this.replayPending || !["connected", "http"].includes(this.connection)
      || this.isRunning() || Boolean(this.state.active_operation)
      || Boolean(this.state.deleted_devices_recovery_phase && this.state.deleted_devices_recovery_phase !== "none")
      || Boolean(this.state.docker_build_cache_prune_fence);
  }

  renderInternalIdsPreview(blocked) {
    if (this.acceptedCommandId || this.uncertainCommandId || this.state.active_operation || this.isRunning()) return nothing;
    const rows = this.state.last_internal_ids_rows || [];
    if (!this.state.last_internal_ids_generated_at && !rows.length) return nothing;
    return html`<section class="card wide" data-testid="internal-ids-preview-section">
      <h2>${t("heading.actions_ids")}</h2>
      <p>${t("label.generated_at")} ${this.view.display_times?.last_internal_ids_generated_at || this.state.last_internal_ids_generated_at || ""}</p>
      <p>${(this.state.last_internal_ids_unresolved || []).length} ${t("label.unresolved")}</p>
      ${(this.state.last_internal_ids_unresolved || []).map((item) => html`
        <vaadin-details>
          <vaadin-details-summary slot="summary">${item.alias || item.path || t("label.unresolved")}</vaadin-details-summary>
          <p>${item.path || ""}: ${item.reason || ""}</p>
        </vaadin-details>`)}
      ${rows.map((row) => html`<vaadin-details @opened-changed=${(event) => {
        if (event.detail.value) this.loadInternalIdDiff(row).catch((error) => this.handleCommandError(error));
      }}>
        <vaadin-details-summary slot="summary">
          <vaadin-checkbox aria-label=${`${t("label.migrate")} ${row.path || ""}`}
            .checked=${Boolean(row.selected)}
            ?disabled=${blocked || !row.changes || !this.internalDiffs?.has(row.path)}
            @change=${(event) => { const selected = event.target.checked; event.target.checked = Boolean(row.selected);
              this.issue("select_internal_ids", { preview_id: this.state.last_internal_ids_preview_id,
                path: row.path, diff_sha256: row.diff_sha256, selected }); }}></vaadin-checkbox>
          <code>${row.path || ""}</code>
        </vaadin-details-summary>
        <p>${row.changes || 0} ${t("label.candidates")}</p>
        <p>${row.unresolved || 0} ${t("label.unresolved")}</p>
        ${this.internalDiffs?.has(row.path) ? html`<pre>${this.internalDiffs.get(row.path)}</pre>` : html`<p>${t("notice.load_exact_diff")}</p>`}
      </vaadin-details>`)}
      ${this.actionButton("internal_ids_migrate", t("action.migrate_and_save"), {
        disabled: blocked || !rows.some((row) => row.selected) ||
          rows.some((row) => row.selected && !this.internalDiffs?.has(row.path)),
        payload: { preview_id: this.state.last_internal_ids_preview_id,
          selected: rows.filter((row) => row.selected).map((row) => ({ path: row.path, diff_sha256: row.diff_sha256 })) },
        confirm: t("confirm.internal_ids_migrate"), theme: "primary",
      })}
    </section>`;
  }

  async loadInternalIdDiff(row) {
    if (this.internalDiffs?.has(row.path) || !this.state.last_internal_ids_preview_id || this.mutationBlocked()) return;
    const previewId = this.state.last_internal_ids_preview_id;
    const response = await fetch(`internal-ids-diff-get?preview_id=${encodeURIComponent(previewId)}&path=${encodeURIComponent(row.path)}`);
    const payload = await response.json();
    if (!response.ok || !payload.ok || payload.diff_sha256 !== row.diff_sha256 || previewId !== this.state.last_internal_ids_preview_id) {
      throw new Error(payload.message || "Internal IDs preview changed. Run Check actions IDs again.");
    }
    this.internalDiffs.set(row.path, payload.diff);
    this.requestUpdate();
  }

  toggleAddon(slug, checked) {
    const selected = new Set(this.view.selected_addons || []);
    if (checked) selected.add(slug);
    else selected.delete(slug);
    this.issue("addons", { addon: [...selected] });
  }

  reconcileSelections() {
    const internalKey = this.state.last_internal_ids_preview_id || "";
    if (internalKey !== this.internalSelectionKey) {
      this.internalSelectionKey = internalKey;
      this.internalDiffs = new Map();
    }
    const conflictKey = `${this.state.operation_generation || 0}:${JSON.stringify(this.state.conflicts || [])}`;
    if (conflictKey !== this.conflictSelectionKey) {
      this.conflictSelectionKey = conflictKey;
      this.conflictDiffs = new Map();
    }
  }

  async loadConflictDiff(path) {
    if (this.conflictDiffs.has(path) || this.mutationBlocked()) return;
    const generation = Number(this.state.operation_generation || 0);
    const conflictKey = this.conflictSelectionKey;
    const currentState = this.state;
    const currentDiffs = this.conflictDiffs;
    const response = await fetch(`conflict-diff-get?generation=${generation}&path=${encodeURIComponent(path)}`);
    const payload = await response.json();
    if (Number(this.state.operation_generation || 0) !== generation
      || this.conflictSelectionKey !== conflictKey
      || this.state !== currentState || this.conflictDiffs !== currentDiffs
      || !this.state.conflicts?.includes(path) || this.mutationBlocked()) return;
    if (!response.ok || !payload.ok || payload.generation !== generation) {
      throw new Error(payload.message || t("error.git_conflict_path_not_pending"));
    }
    this.conflictDiffs.set(path, payload.diff);
    this.requestUpdate();
  }

  render() {
    if (!Object.keys(this.view || {}).length) {
      return html`<main><section class="card wide" role="status"><h1>HA Ops</h1><p>Loading…</p></section></main>`;
    }
    const blocked = this.mutationBlocked();
    const pending = Boolean(this.state.deleted_devices_pending_confirmation);
    const saveRetry = Boolean(this.state.save_push_retry_pending);
    const controlsBlocked = blocked || pending || saveRetry;
    const status = pending ? "pending decision" : this.state.last_status || "idle";
    const displayedStatus = this.connection === "unknown" ? "unknown" : status === "success" ? TEXT.statusDone || "done" : status;
    return html`
      <main>
        <div class="top-grid">
          <section class="card control-card">
            <div class="title-row"><h1>${t("title.site")}</h1><div class="header-badges">
              <div class=${`badge ${status}`} data-status-code=${status} data-connection-state=${this.connection} data-testid="status-badge">${displayedStatus}</div>
              <div class="badge version" data-testid="version-badge">${this.backendVersion || ""}</div>
            </div></div>
            <p>${t("site.description")}</p>
            <dl>
              <dt>${t("field.branch")}</dt><dd><code>${this.view.branch || ""}</code></dd>
              <dt>${t("field.manifest")}</dt><dd><code>${this.view.manifest || ""}</code></dd>
              <dt>${t("field.auth_mode")}</dt><dd>${this.view.auth_mode || ""}</dd>
              <dt>${t("field.last_run")}</dt><dd>${this.view.display_times?.last_run_at || this.state.last_run_at || ""}</dd>
            </dl>
            <p id="client-status" class="client-status" role="status">${this.clientError}</p>
            ${this.state.post_apply_save_recommended ? html`<div class="post-apply-alert" role="status">
              <strong>${t("notice.post_apply_save_title")}</strong>
              <span>${t("notice.post_apply_save")}</span>
            </div>` : nothing}
            <div class="actions">
              <section class="action-section"><h2>${t("heading.ha_to_git")}</h2><div class="action-row">
                ${this.actionButton("save_preview", this.state.post_apply_save_recommended ? t("action.review_post_apply_save") : t("action.preview_save"),
                  { disabled: controlsBlocked, theme: this.state.post_apply_save_recommended ? "warning" : "secondary" })}
              </div>
              ${this.state.post_apply_save_recommended ? html`<p class="muted">${t("notice.post_apply_save_button")}</p>` : nothing}
              <vaadin-checkbox .label=${t("label.include_redundant_data")} .checked=${Boolean(this.state.include_redundant_data)} ?disabled=${controlsBlocked}
                @change=${(event) => { const requested = event.target.checked; event.target.checked = Boolean(this.state.include_redundant_data);
                  this.issue("include_redundant_data", requested ? { include_redundant_data: "on" } : {}); }}>
              </vaadin-checkbox></section>
              <section class="action-section"><h2>${t("heading.git_to_ha")}</h2><div class="action-row">
                ${this.actionButton("preview", t("action.preview_apply"), { disabled: controlsBlocked })}
              </div></section>
              <section class="action-section"><h2>${t("heading.reset_git_state")}</h2><div class="action-row">
                ${this.actionButton("reset_git_state", t("action.reset_git_state"), { disabled: controlsBlocked, confirm: t("confirm.reset_git_state") })}
              </div></section>
              <section class="action-section"><h2>${t("heading.disk_usage")}</h2><div class="action-row">
                ${this.actionButton("disk_usage", t("action.check_disk_usage"), { disabled: controlsBlocked })}
                ${this.actionButton("docker_build_cache_prune", t("action.clear_docker_build_cache"), {
                  disabled: controlsBlocked || !this.view.docker_build_cache?.available,
                  confirm: t("confirm.docker_build_cache_prune"),
                })}
              </div></section>
              ${!this.view.docker_build_cache?.available ? html`<p class="muted" role="status">${this.view.docker_build_cache?.reason || ""} ${this.view.docker_build_cache?.remedy || ""}</p>`
                : saveRetry ? html`<p class="muted" role="status">${t("docker_prune.disabled.save_retry")}</p>`
                  : this.state.docker_build_cache_prune_fence ? html`<p class="muted" role="status">${t("docker_prune.disabled.fence")}</p>` : nothing}
              <section class="action-section"><h2>${t("heading.deleted_devices")}</h2><div class="action-row">
                ${this.actionButton("deleted_devices_preview", t("action.check_deleted_devices"), { disabled: controlsBlocked })}
              </div></section>
              <section class="action-section"><h2>${t("heading.retained_devices")}</h2><div class="action-row">
                ${this.actionButton("retained_devices_preview", t("action.check_retained_devices"), { disabled: controlsBlocked })}
              </div></section>
              <section class="action-section"><h2>${t("heading.actions_ids")}</h2>
                <p class="muted">${t("notice.internal_ids_flow")}</p><div class="action-row">
                ${this.actionButton("internal_ids_preview", t("action.check_actions_ids"), { disabled: controlsBlocked })}
              </div></section>
            </div>
          </section>
          <section class="card details-card"><div class="details-header"><h2>${t("heading.log")}</h2></div>
            <ha-ops-log .lines=${[...(this.state.last_details || []),
              ...((this.state.last_message && this.state.last_details?.at(-1) !== this.state.last_message)
                ? [this.state.last_message] : [])]} .status=${this.state.last_status || "idle"}></ha-ops-log>
          </section>
        </div>
        ${this.state.active_operation?.phase === "recovery_required" ? html`
          <section class="card wide" role="alert" data-testid="operation-recovery">
            <h2>${t("heading.manual_recovery")}</h2>
            <p>${t("notice.operation_uncertain").replace("{command}", this.state.active_operation.command || "Operation")}</p>
            <p>${this.state.active_operation.evidence?.guidance || this.state.active_operation.message || t("notice.inspect_affected_state")}</p>
            ${this.state.active_operation.evidence?.kind ? html`<p>${t("recovery.observed_kind", { kind: this.state.active_operation.evidence.kind.replaceAll("_", " ") })}</p>` : nothing}
            ${Number.isInteger(this.state.active_operation.evidence?.observed_path_count) ? html`
              <p>${t("recovery.observed_paths", { count: this.state.active_operation.evidence.observed_path_count })}</p>` : nothing}
            ${typeof this.state.active_operation.evidence?.refs_match_pre === "boolean" ? html`
              <p>${t("recovery.pre_refs_match", { value: t(this.state.active_operation.evidence.refs_match_pre ? "text.yes" : "text.no") })}</p>` : nothing}
            ${this.state.active_operation.evidence?.optional_snapshot_recorded ? html`
              <p>${t("recovery.snapshot_available", { value: t(this.state.active_operation.evidence.optional_snapshot_available ? "text.yes" : "text.no") })}</p>` : nothing}
            ${this.state.active_operation.evidence?.affected_targets?.length ? html`
              <p>${t("label.affected_targets").replace("{targets}", this.state.active_operation.evidence.affected_targets.join(", "))}</p>` : nothing}
            ${this.state.active_operation.ack_available ? this.actionButton(
              "acknowledge_recovery", t("action.acknowledge_recovery"), {
                payload: { operation_id: this.state.active_operation.command_id,
                  evidence_token: this.state.active_operation.evidence_token },
                confirm: t("confirm.acknowledge_recovery"),
              },
            ) : nothing}
            ${this.state.active_operation.retry_available ? this.actionButton(
              "retry_interrupted_save", t("action.retry_interrupted_save"), {
                payload: { operation_id: this.state.active_operation.command_id,
                  evidence_token: this.state.active_operation.evidence_token },
                confirm: t("confirm.retry_interrupted_save"),
              },
            ) : nothing}
          </section>` : nothing}
        ${this.state.conflicts?.length && !this.state.active_operation ? html`
          <section class="card wide" data-testid="git-conflicts" role="group" aria-label=${t("heading.git_conflicts")}>
            <h2>${t("heading.git_conflicts")}</h2>
            <p>${this.state.conflict_type === "save_unknown_base"
              ? t("notice.conflict_resolution", { ha_choice: t("action.use_ha_version"), git_choice: t("action.use_git_version") })
              : t("message.resolve_git_conflicts")}</p>
            ${(this.state.conflicts || []).map((path) => html`
              <vaadin-details @opened-changed=${(event) => {
                if (event.detail.value) this.loadConflictDiff(path).catch((error) => this.handleCommandError(error));
              }}>
                <vaadin-details-summary slot="summary"><code>${path}</code></vaadin-details-summary>
                ${this.conflictDiffs.has(path) ? html`<pre aria-label=${t("title.conflict_diff")}>${this.conflictDiffs.get(path)}</pre>`
                  : html`<p>${t("message.loading_diff")}</p>`}
                <div class="action-row">
                  ${this.actionButton("resolve_conflict", t("action.use_git_version"), {
                    disabled: controlsBlocked || !this.conflictDiffs.has(path), payload: { path, choice: "git" },
                  })}
                  ${this.actionButton("resolve_conflict", t("action.use_ha_version"), {
                    disabled: controlsBlocked || !this.conflictDiffs.has(path), payload: { path, choice: "ha" },
                  })}
                </div>
              </vaadin-details>`)}
            ${this.state.conflict_type === "save_unknown_base" ? this.actionButton(
              "approve_save_conflicts", t("action.use_ha_for_all_conflicts"), {
                disabled: controlsBlocked || this.state.conflicts.some((path) => !this.conflictDiffs.has(path)),
                confirm: t("action.use_ha_for_all_conflicts"),
              },
            ) : nothing}
          </section>` : nothing}
        ${this.view.docker_prune_recovery && this.view.docker_prune_recovery.kind !== "idle" ? html`
          <section class="card wide" role="alert" data-testid="docker-prune-recovery">
            <h2>${t("heading.disk_usage")}</h2>
            <p>${this.view.docker_prune_recovery?.kind === "corrupt"
              ? t("message.docker_prune_ambiguity_corrupt")
              : this.view.docker_prune_recovery?.phase === "accepted"
                ? t("message.docker_prune_phase_accepted")
                : this.view.docker_prune_recovery?.phase === "dispatching"
                  ? t("message.docker_prune_phase_dispatching")
                  : t("message.docker_prune_ambiguity_valid")}</p>
            ${this.view.docker_prune_recovery?.phase === "resolution_required" ? this.actionButton(
              "docker_build_cache_prune_resolve", t("action.acknowledge_docker_prune"), {
                disabled: Boolean(this.acceptedCommandId || this.uncertainCommandId || this.replayPending)
                  || !["connected", "http"].includes(this.connection),
                payload: this.view.docker_prune_recovery.kind === "corrupt"
                  ? { mode: "corrupt", recovery_token: this.view.docker_prune_recovery.recovery_token }
                  : { mode: "operation", operation_id: this.view.docker_prune_recovery.operation_id },
              },
            ) : nothing}
          </section>` : nothing}
        <div id="reactive-previews" data-testid="reactive-previews">${this.previewTemplate()}</div>
        ${this.renderInternalIdsPreview(controlsBlocked)}
        <section class="card wide"><h2>${t("heading.git_access")}</h2>
          <p>${this.view.auth_mode || ""}</p>
          ${this.actionButton("generate_key", t("action.generate_deploy_key"), { disabled: controlsBlocked })}
        </section>
        <section class="card wide" data-testid="managed-targets-section"><h2>${t("heading.managed_targets")}</h2>
          <p>${t("notice.managed_targets")}</p>
          <vaadin-details .opened=${this.managedTargetsOpen}
            @opened-changed=${(event) => { this.managedTargetsOpen = event.detail.value; }}>
            <vaadin-details-summary slot="summary">${t("label.show_managed_targets")}</vaadin-details-summary>
            <div class="table-scroll"><table class="managed-targets-table"><thead><tr><th>${t("label.managed")}</th><th>${t("label.target")}</th><th>${t("label.type")}</th><th>${t("label.source")}</th></tr></thead><tbody>
            ${(this.view.targets || []).map((target) => html`<tr><td></td><td><code>${target.id || ""}</code></td><td>${target.type || ""}</td><td>${target.source || ""}</td></tr>`)}
            ${(this.view.addons || []).map((addon) => html`<tr><td><vaadin-checkbox aria-label=${`${t("label.managed")} ${addon.name}`}
              .checked=${(this.view.selected_addons || []).includes(addon.slug)} ?disabled=${controlsBlocked}
              @change=${(event) => { const requested = event.target.checked;
                event.target.checked = (this.view.selected_addons || []).includes(addon.slug);
                this.toggleAddon(addon.slug, requested); }}></vaadin-checkbox></td><td>${addon.name}</td><td>${t("label.addon")}</td><td>${addon.slug}</td></tr>`)}
            </tbody></table></div>
          </vaadin-details>
        </section>
        <section class="card wide"><h2>${t("heading.release_snapshots")}</h2>
          <p>${t("notice.release_snapshots")}</p>
          ${(this.view.releases || []).map((release) => html`<div class="action-row">
            <code>${release.name}</code><span>${release.created_at || ""}</span>
            ${this.actionButton("rollback", t("action.rollback"), { disabled: controlsBlocked, payload: { release: release.name }, confirm: t("confirm.rollback") })}
          </div>`)}
        </section>
      </main>
      <vaadin-confirm-dialog
        .opened=${this.confirmOpen}
        .message=${this.confirmMessage}
        .confirmText=${TEXT.confirm}
        cancel-button-visible
        @confirm=${this.confirmMutation}
        @cancel=${() => { this.confirmOpen = false; this.confirmCommand = null; }}
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
    sync();
  }

  onCommand = (event) => {
    event.stopPropagation();
    const { command, payload } = event.detail || {};
    this.dispatchCommand(command, new URL(command.replaceAll("_", "-"), baseUrl()).href, payload || {})
      .catch((error) => this.handleCommandError(error));
  };

  confirmMutation = () => {
    const command = this.confirmCommand;
    this.confirmOpen = false;
    this.confirmCommand = null;
    if (command) this.issue(command.command, command.payload);
  };

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
      const result = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject, sent: false, commandId: envelope.command_id }));
      const entry = this.pending.get(id);
      socket.send(JSON.stringify({ id, ...envelope }));
      entry.sent = true;
      const response = await result;
      if (!response.ok) throw new Error(response.message || "Command rejected");
      this.acceptedCommandId = envelope.command_id;
      this.reconcileAcceptedCommand();
      this.requestUpdate();
      if (TERMINAL_STATE_SYNC_COMMANDS.has(command)) await this.pollCommandState(envelope.command_id);
      if (["preview", "save_preview"].includes(command)) {
        await this.pollCommandState(envelope.command_id, 120000, 1000);
      }
      return response;
    }
    if (WS_COMMANDS.has(command) && socket && socket.readyState !== window.WebSocket?.CLOSED) {
      throw new Error("Connection state is unknown; the command was not retried.");
    }
    const response = await fetch(action, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify(envelope),
    });
    const resultPayload = await response.json();
    if (!response.ok || !resultPayload.ok) throw new Error(resultPayload.message || "Command rejected");
    this.acceptedCommandId = envelope.command_id;
    this.reconcileAcceptedCommand();
    this.requestUpdate();
    await this.pollHttpCommand(envelope.command_id);
    return resultPayload;
  }

  async pollHttpCommand(commandId) {
    await this.pollCommandState(commandId);
    if (!this.socket || this.socket.readyState !== window.WebSocket.OPEN) this.setConnection("http");
  }

  async pollCommandState(commandId, timeoutMs = 10000, intervalMs = 100) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const response = await fetch("api/v1/state");
      if (!response.ok) throw new Error("Could not refresh HA Ops state.");
      this.applyBaseline(await response.json());
      const status = this.state.command_records?.[commandId]?.status;
      if (status === "terminal") return;
      if (status === "failed_unknown") throw new Error("Command outcome is unknown.");
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
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
        if (pending.sent) this.uncertainCommandId = pending.commandId;
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
      const response = await fetch("api/v1/state");
      const snapshot = await response.json();
      if (!this.applyBaseline(snapshot)) return;
      this.replayPending = false;
      this.setConnection("http");
      this.scheduleHttpPoll();
    } catch (error) {
      this.setConnection("unknown");
      this.markUnknown(error);
    }
  }

  scheduleHttpPoll() {
    if (this.httpPollTimer) clearTimeout(this.httpPollTimer);
    if (!this.shouldReconnect || this.connection !== "http") return;
    this.httpPollTimer = setTimeout(async () => {
      this.httpPollTimer = null;
      try {
        const response = await fetch("api/v1/state", { cache: "no-store" });
        if (!response.ok) throw new Error("Could not refresh HA Ops state.");
        this.applyBaseline(await response.json());
        if (this.connection !== "unknown") this.scheduleHttpPoll();
      } catch (error) {
        this.markUnknown(error);
      }
    }, 2000);
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
    if (!frame.state) return false;
    if (frame.schema_version !== 1) {
      this.markUnknown(new Error("Incompatible HA Ops response; reload the page."));
      return false;
    }
    const incomingRevision = Number(frame.revision ?? frame.state_revision ?? frame.state.state_revision ?? 0);
    if (!Number.isSafeInteger(incomingRevision) || incomingRevision < 0) {
      this.markUnknown(new Error("Invalid HA Ops state revision; reload the page."));
      return false;
    }
    if (incomingRevision < this.revision) return false;
    TEXT = Object.fromEntries(Object.entries(TEXT_KEYS).map(([name, key]) => [name, frame.text?.[key] || key]));
    TEXT.catalog = frame.text || {};
    this.view = frame.view || {};
    if (!this.clientVersion && knownVersion(frame.backend_version)) this.clientVersion = String(frame.backend_version);
    this.observeBackendVersion(frame.backend_version);
    this.state = normalizePendingDeletedDevicesState(structuredClone(frame.state));
    this.clientError = "";
    this.reconcileAcceptedCommand();
    this.reconcileSelections();
    this.revision = incomingRevision;
    return true;
  }

  applyPatch(frame) {
    if (frame.schema_version !== 1) {
      this.markUnknown(new Error("Incompatible HA Ops response; reload the page."));
      return;
    }
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
    this.reconcileAcceptedCommand();
    this.reconcileSelections();
    if (frame.view) this.view = frame.view;
    if (frame.text) {
      TEXT = Object.fromEntries(Object.entries(TEXT_KEYS).map(([name, key]) => [name, frame.text[key] || key]));
      TEXT.catalog = frame.text;
    }
    this.revision = revision;
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

  isRunning() {
    return this.state.last_status === "running" || Object.values(this.state.command_records || {})
      .some((record) => ["accepted", "running", "failed_unknown"].includes(record.status));
  }

  reconcileAcceptedCommand() {
    if (this.uncertainCommandId) {
      const record = this.state.command_records?.[this.uncertainCommandId];
      if (record?.status === "terminal") {
        this.uncertainCommandId = null;
        this.clientError = "";
      } else if (record?.status === "accepted" || record?.status === "running") {
        this.acceptedCommandId = this.uncertainCommandId;
      }
    }
    if (!this.acceptedCommandId) return;
    const record = this.state.command_records?.[this.acceptedCommandId];
    if (record?.status === "terminal") this.acceptedCommandId = null;
  }

  isPreviewGenerationRunning() {
    const runningStatuses = new Set(["accepted", "running", "failed_unknown"]);
    if (["preview", "save_preview"].includes(this.state.last_action) && this.state.last_status === "running") return true;
    return Object.values(this.state.command_records || {})
      .some((record) => ["preview", "save_preview"].includes(record.command) && runningStatuses.has(record.status));
  }

  previewTemplate() {
    if (this.acceptedCommandId || this.uncertainCommandId || this.state.active_operation || this.isRunning()) return nothing;
    const hasApplyPaths = Boolean(this.state.last_preview_paths?.length);
    const hasSavePaths = Boolean(this.state.last_save_preview_paths?.length);
    const previewRunning = this.isPreviewGenerationRunning();
    const cleanupRunning = hasCommandInFlight(this.state, ["deleted_devices_preview", "retained_devices_preview", "deleted_devices_delete", "retained_devices_delete", "internal_ids_preview", "internal_ids_migrate"]);
    const pendingDeletedCleanup = Boolean(this.state.deleted_devices_pending_confirmation);
    const hasDeletedPreview = Boolean(this.state.last_deleted_devices_generated_at);
    const hasRetainedPreview = Boolean(this.state.last_retained_devices_generated_at);
    const visible = hasApplyPaths || hasSavePaths || previewRunning || hasDeletedPreview || hasRetainedPreview || cleanupRunning || pendingDeletedCleanup;
    if (!visible) return nothing;
    const loading = previewRunning && !hasApplyPaths && !hasSavePaths;
    return html`
      ${this.renderDeletedPreview(cleanupRunning)}
      ${this.renderRetainedPreview(cleanupRunning)}
      <section class="card wide" data-testid="diff-section">
        <h2>${TEXT.changeList}</h2>
        ${loading
          ? html`<div role="status">${TEXT.loadingPreviewDiff || "Loading Diff..."}</div>`
          : html`
              ${hasApplyPaths ? html`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} .generatedAt=${this.view.display_times?.last_diff_generated_at || ""} direction="apply"
                @ha-ops-command=${this.onCommand}></ha-ops-preview>` : nothing}
              ${hasSavePaths ? html`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} .generatedAt=${this.view.display_times?.last_save_diff_generated_at || ""} direction="save"
                @ha-ops-command=${this.onCommand}></ha-ops-preview>` : nothing}
            `}
      </section>
    `;
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
            ${this.actionButton("deleted_devices_confirm", TEXT.confirmChanges || "Confirm Changes", { disabled: this.mutationBlocked(), theme: "primary" })}
            ${this.actionButton("deleted_devices_revert", TEXT.revertDeletedDevices || "Revert Changes", { disabled: this.isRunning() })}
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
        <p>${TEXT.generatedAt} <span data-transient="deleted-devices-generated">${this.view.display_times?.last_deleted_devices_generated_at || this.state.last_deleted_devices_generated_at || ""}</span></p>
        <div data-transient="deleted-devices-preview">${tree ? renderDeletedDevicesTree(tree) : renderDeletedDevicesTable(rows)}</div>
        ${count > 0 ? html`
          <div class="actions deletion-actions"><div class="action-row">
            ${this.actionButton("deleted_devices_delete", TEXT.removeDeletedEntries, { disabled, confirm: confirmMessage, theme: "primary" })}
          </div></div>
        ` : nothing}
      </section>
    `;
  }

  renderRetainedPreview(cleanupRunning) {
    const rows = this.state.last_retained_devices_rows || [];
    const selected = rows.filter((row) => row.selected).map((row) => row.identity);
    const visible = Boolean(this.state.last_retained_devices_generated_at) || cleanupRunning && this.state.last_action === "retained_devices_preview";
    if (!visible) return nothing;
    const disabled = this.isRunning() || Boolean(this.state.deleted_devices_pending_confirmation) || !rows.length || !this.state.last_retained_devices_fingerprint;
    return html`
      <section class="card wide" data-testid="retained-devices-preview-section">
        <h2>${TEXT.retainedDevicesPreview}</h2>
        <p class="muted">${TEXT.retainedPreviewNotice}</p>
        <p class="muted">${TEXT.retainedDeleteNotice}</p>
        <p>${TEXT.generatedAt} <span data-transient="retained-devices-generated">${this.view.display_times?.last_retained_devices_generated_at || this.state.last_retained_devices_generated_at || ""}</span></p>
          <div data-transient="retained-devices-preview">${renderRetainedDevicesTable(rows, disabled, (identity, checked) => this.issue("select_retained_device", {
            retained_preview_fingerprint: this.state.last_retained_devices_fingerprint || "",
            retained_preview_generated_at: this.state.last_retained_devices_generated_at || "",
            identity, selected: checked,
          }))}</div>
          ${rows.length ? html`<div class="actions deletion-actions"><div class="action-row">
            ${this.actionButton("retained_devices_delete", TEXT.deleteRetainedDevices, {
              disabled: disabled || !selected.length,
              payload: {
                retained_preview_fingerprint: this.state.last_retained_devices_fingerprint || "",
                retained_preview_generated_at: this.state.last_retained_devices_generated_at || "",
                candidate: selected,
              },
              confirm: TEXT.confirmRetainedDevicesDelete, theme: "primary",
            })}
          </div></div>` : nothing}
      </section>
    `;
  }

  markUnknown(error) {
    this.setConnection("unknown");
    this.clientError = error.message;
    this.requestUpdate();
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
    this.clientError = message;
    this.updateStatusBadge();
  }

  setConnection(connection) {
    if (connection !== "http" && this.httpPollTimer) {
      clearTimeout(this.httpPollTimer);
      this.httpPollTimer = null;
    }
    this.connection = connection;
    this.updateStatusBadge();
  }

  isDegradedConnection() {
    return ["reconnecting", "http", "unknown"].includes(this.connection);
  }

  updateStatusBadge() {
    this.requestUpdate();
  }
}
customElements.define("ha-ops-app", HaOpsApp);
