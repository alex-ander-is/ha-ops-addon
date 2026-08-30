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
