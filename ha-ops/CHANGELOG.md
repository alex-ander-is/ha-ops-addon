# Changelog

## 0.4.28

- Show Git-to-HA preview target progress in the UI while preview is running.
- Mark stale running actions as interrupted after HA Ops restarts.

## 0.4.27

- Build Git-to-HA previews from managed config baselines instead of copying and diffing the full live trees.

## 0.4.26

- Add startup exception logging and per-target apply preview progress logs.

## 0.4.25

- Render Save Preview as a single field and avoid duplicate "No Save changes" blocks.

## 0.4.24

- Add spacing between stacked preview fields in the ingress UI.

## 0.4.23

- Align add-on presentation assets with Home Assistant expectations: keep a 128x128 app icon and use a landscape logo.
- Document that update indicators come from Supervisor version state, not from add-on image assets.

## 0.4.22

- Restore the add-on app icon for Home Assistant apps/add-ons lists.

## 0.4.21

- Make managed add-on checkboxes save immediately and remove the separate Save Add-on Selection button.

## 0.4.20

- Replace the always-rendered Save Candidates block with an explicit Preview HA to Git action.
- Preview HA to Git builds the Save result in a temporary tree and shows changes without commit or push.

## 0.4.19

- Add a persistent Save Candidates section that shows live config files eligible for Save HA to Git.

## 0.4.18

- Removed the add-on app icon while keeping the logo.

## 0.4.17

- Show Save export candidate files before the unknown-base conflict gate and keep them visible after resolving Save conflicts.

## 0.4.16

- Show the Git file change list prepared by Save HA to Git before commit and push.

## 0.4.15

- Export `core.config_entries` as a redacted managed projection instead of raw `.storage`.
- Apply managed config entry projections by merging only allowlisted safe fields into existing entries.

## 0.4.14

- Add HA Ops add-on icon and logo assets.

## 0.4.13

- Store unresolved Git conflicts as a `conflicts` state instead of a generic error state.

## 0.4.12

- Show `CONFLICTS` instead of `ERROR` when unresolved Git conflicts require user choice.

## 0.4.11

- Highlight changed fragments inside modified conflict diff lines.

## 0.4.10

- Add an optional line wrapping toggle for conflict diff blocks.

## 0.4.9

- Add colored conflict diff highlighting.
- Keep long conflict lines inside horizontally scrollable diff blocks.

## 0.4.8

- Explain Git conflict choices in the UI.
- Show conflict details before choosing between HA and Git versions.

## 0.4.7

- Add safety coverage for AppContext, Git auth, conflict handling, and HTTP handler wiring.
- Move Git auth, conflict resolution, app context, and web handler logic out of the server entrypoint.
- Keep `server.py` as a composition layer while preserving existing routes and UI behavior.

## 0.4.6

- Split server internals into UI, state, supervisor, backups, manifest, Git, sync, jobs, and target modules.
- Avoid stopping Home Assistant Core twice when Apply fails after Core was already stopped for `.storage` sync.
- Recursively remove excluded runtime files from add-on save destinations before exporting live config.
- Reject add-on manifest live paths outside expected add-on config roots.
- Filter release snapshots to managed config paths to avoid storing runtime databases and logs.
- Add regression coverage for rollback, delete semantics, protected storage, conflict blocking, and clean checkout imports.

## 0.4.5

- Apply selected add-ons as an overlay by default so partial Git sources do not delete live-only add-on files.
- Keep destructive add-on deletes available only through explicit `delete: true`.

## 0.4.4

- Run Home Assistant config check after syncing and before starting Core when `.storage` apply stopped Core.

## 0.4.3

- Reject empty, absolute, current-directory, and parent-escaping `repo_path` values before touching the local checkout.

## 0.4.2

- Clean untracked and ignored files from the local checkout before reading Git sources or saving live config.
- Prevent stale files left in `/data/ha-config` from affecting Preview, Apply, or Save.

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
