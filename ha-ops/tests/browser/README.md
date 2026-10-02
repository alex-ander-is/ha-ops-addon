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

The decision runner uses fresh disposable previews and the shipped static bundle.
It verifies local native checkbox and HA/Git controls, immediate Confirm state,
zero edit dispatch or transport, retained expanded semantic/raw diff nodes,
two-axis scroll, keyboard focus and Save subject, reload-empty decisions, and
one complete final Apply/Save batch over WebSocket and HTTP. Real final commands
are held by the local harness to inspect mounted disabled review content.

The backup runner verifies mounted local review through a typed refusal,
local-edit dismissal, empty reload decisions with no actionable old continuation,
normal Retry, fresh Retry and acknowledgement. Existing shared pages and profile
are retained. Each runner records its exact harness PID/root and page ownership
in `runtime.json`; preserve evidence before stopping only its recorded fixture.
