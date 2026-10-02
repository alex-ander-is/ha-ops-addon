# HA Ops local browser smoke

Run:

```bash
PLAYWRIGHT_SHARED_ROOT=/Users/purportex/Applications/Playwright node ha-ops/tests/browser/run.mjs
```

The runner starts `ha-ops/dev_harness.py` on `127.0.0.1` with an ingress-like
base URL, temporary `data/`, `homeassistant/`, `addon_configs/`, a fake Git
remote, and a fake Supervisor. Scenarios use HA Ops host-level test IDs and
accessible names to cover Lit/Vaadin bootstrap, WebSocket preview submission,
state revision replay, reconnect, lazy per-file `diff-get`, debug snapshot
redaction, disabled/running UI, mobile layout, and HTTP dispatch when WebSocket
is unavailable before send.

Out of scope for this smoke: real Supervisor ingress proxying, live Home
Assistant backups, Core restart/reload, App lifecycle changes, and writes to
the real HA config or user Git remotes.

## Backup continuation flow

Run the focused visible-browser flow with the shared Playwright runtime:

```bash
/Users/purportex/Applications/Playwright/bin/playwright-node ha-ops/tests/browser/backup-continuation.mjs
```

It uses the shared persistent Chrome profile, preserves existing pages, and
leaves its temporary harness and browser open for inspection. If that profile
is already running with CDP enabled, set `HA_OPS_BROWSER_CDP_URL` to its local
endpoint. Screenshots and DOM evidence are written to the temporary directory printed
by the runner (override with `HA_OPS_BROWSER_ARTIFACTS_DIR`). The fixture simulates missing/fresh policy
results; the real backup gate and Apply job are covered by
`test_backup_continuation.py` using temporary local Git/config fixtures.

The browser flow exercises refusal, normal Retry, fresh Retry, and one-attempt
acknowledgement over WebSocket and HTTP fallback, plus retained decisions,
reload, connection blocking, Vaadin controls, footer layout, and disabled
styles. It makes no live Supervisor or user Git remote calls.

## Preview decision continuity

Run the focused rendered regression with the shared Playwright runtime:

```bash
/Users/purportex/Applications/Playwright/bin/playwright-node ha-ops/tests/browser/preview-decisions.mjs
```

The flow covers Apply and Save selection and HA/Git choices over WebSocket and
HTTP, mounted expanded diff continuity, mutation fencing, focus settlement and
content invalidation. It uses disposable local fixtures and the shipped static
bundle. Set `HA_OPS_BROWSER_CDP_URL` to reuse an already running shared browser.
It preserves existing pages and leaves the temporary harness and browser open.
Screenshots and DOM evidence go to the printed temporary directory; override
it with `HA_OPS_BROWSER_ARTIFACTS_DIR`. `HA_OPS_BROWSER_BUNDLE_OVERRIDE` serves a
disposable historical bundle for regression proof without changing App files.

The separate `test_preview_decisions.py` group runs seven complete-module Node
scenarios with inert Lit/Vaadin imports. It explicitly skips when Node is
unavailable and does not replace this rendered flow. Use
`HA_OPS_FRONTEND_SOURCE` to select a disposable historical source for those
logic regressions.

The decision runner prepares a fresh real preview for each direction and
transport. Its ordinary cases use the real local backend. Delayed cases pause
transport and seed a direction/generation-specific preview ID before expanding
loaded content; every decision envelope is captured, never sent to the backend.
The fixture fences the actual production `receive` and `applyBaseline` entries,
including already pending HTTP state responses, and pauses future polling.
Simulated WebSocket acknowledgements use a saved production receiver directly.
Same/new-revision late frames are delivered through actual socket MessageEvents
and the HTTP baseline entry while loaded and while a command is pending; seeded
state, intent, focus and loaded content must stay identical. This simulated
authority fixture covers all 28 delayed actions and checks the
actual `selected` wire values (`'1'`/`''`), unchanged authoritative selection and
choices during nonterminal phases, and independently expected terminal choices.
Semantic and raw templates must both overflow and retain nonzero two-axis scroll.
Save subject preservation is mandatory in Save cases. Native keyboard focus,
Tab/pointer cancellation, synchronous fences, detached refusal and intentional
cursor/reload invalidation remain part of each direction/transport flow. The
complete-module tests cover source-only mixed/unknown/recovery and lazy-fetch
edges; the browser report does not claim those unexecuted scenarios.

For historical checkbox proof, use the same minimal named probe against the
current shipped bundle and a disposable baseline override. It loads genuine
Apply diffs, clicks the native Vaadin checkbox, and observes exact node/content
continuity across frames and shadow-tree mutations, without synthetic review
content or preview IDs:

```bash
HA_OPS_BROWSER_CDP_URL=http://127.0.0.1:9227 HA_OPS_BROWSER_PROBE=checkbox-baseline \
  /Users/purportex/Applications/Playwright/bin/playwright-node ha-ops/tests/browser/preview-decisions.mjs
git show 3907d12:ha-ops/app/static/ha-ops.js > /private/tmp/ha-ops-checkbox-baseline-3907d12.js
HA_OPS_BROWSER_CDP_URL=http://127.0.0.1:9227 HA_OPS_BROWSER_PROBE=checkbox-baseline \
  HA_OPS_BROWSER_BASELINE_REVISION=3907d12 \
  HA_OPS_BROWSER_BUNDLE_OVERRIDE=/private/tmp/ha-ops-checkbox-baseline-3907d12.js \
  /Users/purportex/Applications/Playwright/bin/playwright-node ha-ops/tests/browser/preview-decisions.mjs
```

The current probe must exit zero. The baseline must exit nonzero specifically
with `original checkbox collapsed or detached mounted diff`, with before/after
screenshots and `checkbox-baseline.json` showing the collapse/removal. A setup
failure or arbitrary nonzero exit is not regression proof.
