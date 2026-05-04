# Changelog

## 0.4.1

- Make Managed Add-ons checkboxes control add-on targets even when `ha-ops.json` exists.
- Preserve manifest options for selected add-ons while excluding unchecked add-ons.
- Save selected add-ons from live config even when Git currently contains only a `.gitkeep` scaffold.

## 0.4.0

- Replace the export-branch flow with `Save HA to Git`, `Preview Git to HA`, and `Apply Git to HA`.
- Make empty or partial Git repositories safe: Apply is an overlay/no-op and does not delete live-only config.
- Add installed add-on discovery with managed add-on checkboxes.
- Add file-level Git conflict resolution for save conflicts.
- Keep commit metadata out of the main status UI because live Home Assistant can change outside Git.

## 0.3.20

- Require a fresh system backup before Apply and create a full system backup when needed.
- Require the backup to be stored in a configured backup location by default.
- Require Apply to match the last Preview Apply commit and diff fingerprint.
- Block Apply when previewed deletions exceed the configured limit.
- Skip protected `.storage` files unless a target explicitly sets `allow_protected_storage`.
- Prune local release snapshots by count and age.

## 0.3.19

- Add an Apply Preview action that shows a diff before applying Git config.
- Show latest Home Assistant backup status in the UI.
- Rename the action button to `Pull & Apply`.
- Use checksum-based sync so same-size config edits are not skipped.
- Skip optional add-on targets whose source folder only contains `.gitkeep`.
- Validate manifest paths, rollback release names, and make deploy key regeneration atomic.

## 0.3.18

- Exclude temporary files and `node_modules` symlinks from exported Zigbee2MQTT external converters.

## 0.3.17

- Export and apply config-only Zigbee2MQTT files from Home Assistant config storage.

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
