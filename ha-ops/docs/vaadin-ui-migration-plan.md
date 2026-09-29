# Vaadin UI Migration Plan

Status: implemented in HA Ops 1.0.0; regression coverage expanded in the 1.0.2 patch.

## Goal

Complete UI debt items 1, 2, 3, 4, and 6: remove the hybrid legacy-control
upgrade layer, render the remaining interactive sections with Lit and Vaadin
OSS, update the conflict workflow, migrate the Internal IDs preview, and remove
duplicate server-rendered UI. Keep the existing server command and safety
contracts. JavaScript is required for the ingress UI.

The 2026-09-28 audit approved a static client and a fail-closed command
boundary. The 1.0.0 release shipped the migration. The 1.0.2 patch adds
browser coverage for Save, Apply, conflict choices, Internal IDs controls,
recovery presentation, and HTTP fallback, and protects newer local commits
when retrying or cancelling a failed Save push.

## Current boundaries

- `frontend/src/ha-ops.js` owns the interactive UI and handles WebSocket
  replay, revisions, and HTTP command fallback. The compiled module and CSS
  are served as static ingress-relative assets.
- `app/web.py` serves a static inert shell plus versioned JSON; it does not
  compose request-time interface markup. The historical `app/ui.py` renderer
  is removed; `app/diff_split.py` retains the pure diff parser used by the API.
- `app/state.py` remains authoritative for preview identities, selected paths,
  conflict choices, command revisions, recovery fences, and Internal IDs preview
  lifetime. Preserve `service-branch-merge-contract.md`.
- The frontend currently pins Lit 3.3.3 and Vaadin OSS 25.2.8. Do not add
  Vaadin Commercial Features.

## Architecture

1. Define a versioned, sanitized UI data projection at the `app/web.py`
   boundary. It contains display data and capability/disabled flags, not HTML,
   credentials, private keys, unredacted diagnostics, or internal filesystem
   secrets. Use the same projection for initial load and state/replay updates.
2. Serve `app/static/index.html` as an inert, accessible app shell; deliver
   localization through the JSON projection. Remove server-rendered interactive controls,
   preview fallbacks, and duplicated presentation helpers. Keep HTTP as a
   JavaScript command transport fallback when WebSocket is unavailable.
3. Render all interactive sections declaratively in Lit with native Vaadin OSS
   controls. Mount dynamic sections at stable template locations. Remove
   `upgradeControls()`, form-derived command routing, heading-based preview
   insertion, and hiding of duplicate server markup.
4. Route component actions through the existing command envelope and explicit
   payloads. Preserve server-side validation, idempotency, replay/revision,
   recovery fences, and stale-preview rejection. A disabled client control is
   never the only safety check.
5. Replace legacy conflict presentation and copy, including `Approve HA to
   Git`, with the current Save/Apply choices and confirmation flow. Save requires
   explicit HA/Git choices for selected conflict paths. Apply defaults selected
   conflicts to Git and allows an explicit HA override. Preserve the behavior
   for unselected paths and stale preview identities.
6. Render Internal IDs preview in Lit: expandable exact per-file diffs fetched
   with a current preview ID and path, selected changed paths with diff digests,
   unresolved count, and Vaadin confirmation. The migration command sends
   preview ID and selected path/digest pairs, never row indexes.
7. Migrate the remaining targets, Apps, organizer, redundant data, Git
   access, release snapshots, and cleanup controls in the same pass so the
   page has no server-rendered interactive controls. Keep localized text and
   visibly disabled button styling consistent.
8. Rebuild the committed frontend bundle from source and verify source/bundle
   consistency.

## Verification

- Replace server-HTML assertions with checks for the inert shell, sanitized
  projection, command transport, and server rejection of invalid, stale,
  fenced, missing-choice, and duplicate actions.
- Exercise first paint, WebSocket and HTTP fallback, Save/Apply conflict
  choices, Internal IDs selection and confirmation, running/retry/recovery
  disabled states, and stale preview clearing in the browser harness.
- Run the affected flows at desktop and phone widths with Playwright, inspect
  screenshots and DOM, and fix any mismatch before marking the work done.
- Run focused Python tests and the frontend build/browser smoke tests. Use
  isolated local checks; no live HA Apply, restart, or write is needed.

## Diff viewer research (item 7)

Vaadin OSS has no native advanced diff viewer. The current lightweight Lit
renderer can remain for this migration. `diff2html` 3.4.56 (MIT) is the best
candidate for a separate prototype: it supports unified and side-by-side
views, line numbers, and syntax highlighting. Verify escaping of untrusted
diff text before integration and do not inject raw HTML without a tested
sanitization boundary. Monaco is substantially broader than a read-only diff
viewer; `@git-diff-view/core` is pre-1.0 and its renderer is React-oriented.
Do not add a diff dependency under this plan without a separate decision.

## Risks and scope

The initial page/data contract changes. Test initial load and reconnect against
the same projection before deleting server renderers. Translation gaps and
legacy HTML-dependent tests need explicit migration. Keep the existing Python
business logic, state schema, service-branch merge semantics, and live HA
operations outside this UI migration unless a reviewed finding proves a
change is necessary.
