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
