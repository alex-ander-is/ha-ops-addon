# Changelog

## 0.3.5

- Add explicit Export and Push actions for bootstrapping a Git-backed config from live Home Assistant state on the `export` branch.
- Exclude database, log, backup, deps, and tts files from export by default.

## 0.3.4

- Use Home Assistant theme variables in the ingress UI.
- Use `restart_after_apply` as the default restart behavior for manifest targets without `restart_after_sync`.
- Add English and Russian option translations.
