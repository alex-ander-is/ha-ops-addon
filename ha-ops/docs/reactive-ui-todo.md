# Reactive HA Ops UI ToDo

## Goal

Move HA Ops from server-rendered WebSocket fragments toward a reactive client model without turning the App into a full single-page application.

The intended end state is:

- the server remains the source of truth;
- the client keeps a small explicit state model;
- WebSocket messages carry semantic events and state patches, not full HTML fragments;
- large diff content is loaded separately on demand;
- existing non-JavaScript or degraded fallback paths remain available where practical.

## Recommended UI foundation

Use Lit and native Web Components for the reactive layer.

Reasons:

- Home Assistant’s frontend architecture is already Web Components-oriented, so this aligns with the host ecosystem.
- Lit is small enough for an App ingress UI.
- Components can be introduced incrementally without rewriting the whole page.
- It avoids a full React/SPA toolchain unless HA Ops later grows into a larger application.

Keep the first component set narrow:

- `<ha-ops-app>`: owns client state, WebSocket lifecycle, command dispatch, reconnect/replay handling.
- `<ha-ops-log>`: renders streamed log lines and scroll state.
- `<ha-ops-preview>`: renders active preview metadata, file list, selection state, and confirmation state.
- `<ha-ops-preview-file>`: owns one preview row, expanded/collapsed state, wrap-lines state, conflict choice, and lazy diff loading.
- `<ha-ops-debug-snapshot>`: produces a redacted state snapshot for bug reports.

## Server/client contract

Do not make the browser optimistic by default.

Every command or state transition that is not confirmed by the server stays `pending` or `unknown` in the UI. Optimistic behavior can be added later only for low-risk interactions.

Prefer semantic WebSocket messages:

```json
{ "type": "job_started", "job_id": "...", "command": "preview_ha_to_git" }
{ "type": "log_line", "job_id": "...", "seq": 42, "text": "Preview homeassistant: building diff" }
{ "type": "preview_ready", "job_id": "...", "preview_id": "...", "files": [] }
{ "type": "preview_file_updated", "preview_id": "...", "path": "...", "status": "changed" }
{ "type": "job_finished", "job_id": "...", "status": "success" }
{ "type": "state_patch", "revision": 123, "patch": {} }
```

Keep HTML fragment messages only as a transition bridge, then remove them once the reactive components cover the affected UI.

## Diff loading

Use lazy per-file diff loading for expanded rows.

Rationale:

- `.storage` diffs can be large;
- the same preview state may be replayed after reconnect;
- log/progress events should not resend unchanged diff bodies;
- the user usually expands only a subset of files.

Required behavior:

- preview state contains file metadata and a stable diff reference, not necessarily full diff text;
- expanding a file fetches the diff detail by `preview_id`/generation and file path or cursor;
- the server rejects stale preview/diff references;
- “Diff detail unavailable” is shown only when the server confirms absence or failure, not merely because the current WS state is redacted/lightweight;
- already loaded file diffs may be cached client-side for the current preview only;
- diff cache is cleared when the preview is cancelled, replaced, or the page reloads.

## MVP migration steps

1. Introduce a client state store.
   - Track connection, job, log, preview, selection, pending command, and disabled-control state.
   - Treat server events as the only authority.
   - Preserve current WebSocket reconnect/replay behavior.

2. Convert log rendering.
   - Replace server-rendered log fragments with `log_line` events.
   - Keep overflow behavior: desktop height follows the left column; stacked/mobile fallback is 500px.
   - Keep tests for live log updates before/during/after Preview actions.

3. Convert preview file rendering.
   - Render preview rows from client state.
   - Implement Expand, Collapse, Expand All, Collapse All, Wrap Lines, Select All, Select None in the component layer.
   - Confirm that every state-changing button is disabled during running operations.

4. Add lazy diff detail API.
   - Fetch one file diff on expand.
   - Show loading, loaded, unavailable, and stale states explicitly.
   - Keep large diff bodies out of repeated WS state messages.

5. Convert command dispatch.
   - Use WebSocket RPC for Preview HA to Git, Preview Git to HA, confirm/cancel flows, retained device checks, and action ID checks.
   - Keep old POST/fetch handlers as compatibility fallback until explicitly removed.

6. Add debug snapshot support for the reactive model.
   - Include connection status, current state revision, current job, preview metadata, selected files, expanded files, loaded diff refs, pending commands, and recent log tail.
   - Redact repo URLs, paths, tokens, hostnames, and sensitive values consistently with existing debug redaction.

## Test requirements

Extend the local dev harness and Playwright coverage for:

- initial page load without live HA;
- WebSocket connect, disconnect, reconnect, and replay;
- Preview HA to Git starts, streams log, updates status, disables all state-changing buttons, finishes, and re-enables controls;
- Preview Git to HA starts, streams log, updates status, disables all state-changing buttons, finishes, and re-enables controls;
- Expand/Collapse/Expand All/Collapse All work after preview state arrives through WebSocket;
- lazy diff detail loads when a file is expanded;
- stale diff reference is rejected and displayed as stale/unavailable;
- debug snapshot contains enough state to report a bug without exposing secrets;
- page reload clears transient preview/diff client cache unless the server has an active preview to replay.

## Explicit non-goals for the first reactive pass

- Full SPA router.
- Replacing all server templates.
- Using Home Assistant Core WebSocket.
- Optimistic updates for apply/save operations.
- Real Supervisor ingress emulation in local tests.
- Live HA apply/restart/reload verification.

## Open decisions for the implementation agent

- Whether to bundle Lit from npm during the App build or vendor a pinned browser module.
- Exact state patch format: JSON Merge Patch, JSON Patch, or narrow domain events only.
- Whether full initial state is embedded in HTML or fetched after WebSocket connection.
- How long completed operation logs should remain replayable server-side.
- Whether old fragment messages should be deleted in the same release or after one compatibility release.
