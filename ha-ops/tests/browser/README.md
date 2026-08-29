# HA Ops local browser smoke

Run:

```bash
PLAYWRIGHT_SHARED_ROOT=/Users/purportex/Applications/Playwright node ha-ops/tests/browser/run.mjs
```

The runner starts `ha-ops/dev_harness.py` on `127.0.0.1` with an ingress-like
base URL, temporary `data/`, `homeassistant/`, `addon_configs/`, a fake Git
remote, and a fake Supervisor. It covers WebSocket preview submission, live
state/log fragments, reconnect replay, lazy `diff-get` access, debug snapshot
redaction, disabled/running UI, and fetch fallback with WebSocket unavailable.

Out of scope for this smoke: real Supervisor ingress proxying, live Home
Assistant backups, Core restart/reload, App lifecycle changes, and writes to
the real HA config or user Git remotes.
