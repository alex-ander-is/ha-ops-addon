# Changelog

## 0.3.16

- Force-stage allowlisted Home Assistant `.storage` config files during Export and Push even when repository `.gitignore` ignores `.storage`.

## 0.3.15

- Show `Running...` in Last Run Details while an operation is running and no details are available yet.

## 0.3.14

- Export only Home Assistant config paths instead of downloaded custom components, frontend assets, binaries, secrets, and runtime files.
- Leave non-config Home Assistant paths unmanaged during Apply.

## 0.3.13

- Read the current remote `origin/export` SHA before Push and use an explicit force-with-lease.

## 0.3.12

- Export selected safe Home Assistant `.storage` configuration files.
- Apply selected `.storage` files in overlay mode so auth/session/token files are not deleted.

## 0.3.11

- Show the running HA Ops version in the ingress UI footer.

## 0.3.10

- Recreate the local `export` branch from `origin/main` on every Export.
- Push `origin/export` with `--force-with-lease` so export stays a fresh review branch.

## 0.3.9

- Remove previously exported excluded files from the `export` branch before copying live config.
- Report how many excluded items were cleaned from each export destination.

## 0.3.8

- Push local `export` commits even when the working tree has no uncommitted changes.
- Report whether `origin/export` is missing or already up to date.

## 0.3.7

- Disable Apply, Export, and Push buttons while an action is running.
- Skip `git push` when there are no local export changes to commit.

## 0.3.6

- Reduce exported Home Assistant noise by excluding runtime storage, cache, compiled files, frontend bundles, media cache, and Zigbee2MQTT runtime state.
- Keep Push action output concise in the activity log.

## 0.3.5

- Add explicit Export and Push actions for bootstrapping a Git-backed config from live Home Assistant state on the `export` branch.
- Exclude runtime storage, cache, database, log, backup, deps, frontend bundle, media cache, and tts files from export by default.

## 0.3.4

- Use Home Assistant theme variables in the ingress UI.
- Use `restart_after_apply` as the default restart behavior for manifest targets without `restart_after_sync`.
- Add English and Russian option translations.
