# Changelog

Released sections are immutable. Put every new change into a new version section.

## 0.7.7

- Keep Internal IDs preview log entries in chronological order.
- Commit pending Internal IDs migration changes before later Git actions so HA Ops does not block itself on a dirty checkout.

## 0.7.6

- Remove the duplicate unresolved device blocks summary below the Internal IDs migration table.

## 0.7.5

- Align Internal IDs migration expand controls with their file rows.
- Show unresolved Internal IDs migration YAML blocks inside the expanded file row.
- Show unresolved-only rows as not selectable and make disabled buttons visibly gray.

## 0.7.4

- Remove the extra divider drawn by action-area checkboxes between grouped controls.
- Simplify Internal IDs migration totals and table columns to show only per-file candidates and unresolved counts.
- Combine Internal IDs migration file selection and diff preview into one expandable list.

## 0.7.3

- Clear stale Internal IDs migration previews after refresh, restart, and add-on updates.
- Rename the Internal IDs check action to Check actions IDs and group primary controls by HA to Git, Git to HA, and Maintenance sections.

## 0.7.2

- Keep Internal IDs migration file paths clipped with ellipsis so they do not overlap metric columns.

## 0.7.1

- Improve Internal IDs migration UI table sizing and split check actions into separate rows.
- Avoid duplicate Internal IDs preview progress text while checks are running.

## 0.7.0

- Add an internal id migration flow for HA Ops automations, scripts, and scenes with preview and checkbox approval.

## 0.6.23

- Add a retained devices cleanup flow for stale Zigbee2MQTT MQTT discovery topics with checkbox approval.

## 0.6.22

- Treat Home Assistant entity registry supported_features as redundant runtime data in normalized registry diffs and applies.

## 0.6.21

- Clear stale transient status after an HA Ops add-on version update so old errors are not shown as fresh failures.

## 0.6.20

- Allow organizer-managed automations, scripts, and scenes to be added or removed in Git without comparing against stale live counts.
- Store and recheck a canonical RFC 8785 live automation/script/scene fingerprint between Preview Git to HA and Apply Git to HA.
- Verify that organizer apply writes the same automation/script/scene identities and counts that came from the Git source.

## 0.6.19

- Compare organizer-enabled Git to HA previews in the organized area view to avoid heap YAML rewrite noise.
- Normalize organizer index id order in preview diffs.

## 0.6.18

- Move Include redundant data under the HA to Git actions.
- Clear stale Save previews and Save conflict resolutions when Include redundant data changes.
- Keep Git to HA approval fingerprints stable across diff header timestamp changes.

## 0.6.17

- Preserve live Home Assistant registry hidden fields during Git to HA preview and approved apply.
- Hide normalized registry-only noise from Git to HA previews and storage approval checks.
- Show completed actions as done instead of success.
- Add an Include redundant data toggle for raw 1:1 HA to Git registry saves and previews.

## 0.6.16

- Clear stale preview diffs before starting Preview HA to Git, Preview Git to HA, and Check deleted_devices.
- Add a Git pre-push hook that blocks pushes while the test suite is failing.
- Keep Save commits from changing registry fields and ordering that are hidden from normalized diffs.

## 0.6.15

- Hide the redundant Save Preview diff while Save conflicts are pending.
- Keep Save commits from rewriting unchanged registry entries while normalizing changed registry entries.

## 0.6.14

- Save normalized Home Assistant registry files to Git when real registry changes are committed.

## 0.6.13

- Use the same normalized Home Assistant registry diff in Save conflict details as in Save Preview.

## 0.6.12

- Keep normalized Home Assistant registry Save Preview diffs readable instead of rendering each registry as one long line.

## 0.6.11

- Hide Home Assistant registry noise from Save Preview diff output even when the same registry file has real changes.

## 0.6.10

- Ignore additional Home Assistant registry noise-only changes in Save Preview and Save commits while preserving real registry state changes.
- Explain Release Snapshots before the empty snapshot state.

## 0.6.9

- Ignore Home Assistant registry order-only changes in Save Preview and Save commits.

## 0.6.8

- Keep action results visible after async Save or Apply actions instead of racing them with display-state cleanup.
- Report Save outcomes explicitly as pushed changes or no live changes.

## 0.6.7

- Keep Save and Apply preview diffs complete instead of truncating long output.
- Document the planned device registry connection-order diff stabilization.

## 0.6.6

- Show a colored deleted_devices diff while cleanup is pending so Confirm and Revert have visible consequences.

## 0.6.5

- Show pending deleted_devices cleanup as a decision state instead of a generic error.
- Move action messages from the overview card into the Log panel and remove unclear preview deletion metadata from the overview.

## 0.6.4

- Hide empty Apply Preview and Save Preview sections until their previews exist.
- Allow confirming deleted_devices cleanup after harmless device registry changes when deleted_devices remains empty.
- Merge deleted_devices during Revert so unrelated registry changes and newly added deleted_devices entries are preserved.
- Log startup state and deleted_devices cleanup actions for troubleshooting.

## 0.6.3

- Clear stale deleted_devices preview results on manual refresh unless a cleanup is pending confirmation.

## 0.6.2

- Clear transient success status on manual page refresh and simplify deleted_devices preview columns.

## 0.6.1

- Show deleted_devices candidates as an entity-aware table and keep cleanup actions next to the preview.
- Clear transient conflict UI on manual page refresh while preserving conflicts during internal action reloads.

## 0.6.0

- Add a two-step deleted_devices cleanup flow with preview and approved deletion from the live Home Assistant device registry.

## 0.5.7

- Keep the HA Ops two-column layout active on medium-wide screens and prevent diff content from escaping page padding.

## 0.5.6

- Let the HA Ops page use the full browser width and keep Last Run Details scrollable beside the controls.

## 0.5.5

- Show full conflict details without truncating long diffs.

## 0.5.4

- Use dot-prefixed organizer service buckets `.unknown` and `.mixed`.
- Keep real Home Assistant areas such as `Unknown` separate from service buckets.
- Document stable entity-reference conversion for automation, script, and scene YAML.

## 0.5.3

- Merge managed add-on selection into the targets table.
- Hide the protected-storage implementation detail from the main targets table.

## 0.5.2

- Move the Home Assistant organizer toggle into the main action card so it is visible without scrolling.

## 0.5.1

- Add the Home Assistant organizer UI toggle.
- Stop Git-to-HA when a split organizer view exists in Git but the organizer toggle is off.
- Allow HA-to-Git with the organizer toggle off to convert Git back to heap YAML files.

## 0.5.0

- Add opt-in Home Assistant organizer for UI-managed automations, scripts, and scenes.
- Store organized Git views under `.ha-ops/areas` and compose them back to Home Assistant heap files on Apply.
- Keep organizer migration explicit with a Home Assistant UI toggle.
- Add organizer integrity checks, routing fallbacks, and Save/Apply safety coverage.

## 0.4.38

- Clear stale `Home Assistant config check failed: {'result': 'ok', 'data': {}}` errors left by earlier versions.

## 0.4.37

- Accept the current Supervisor config-check success response so Apply can continue to Core start.
- Avoid stopping Home Assistant Core for no-op managed `core.config_entries` projections.

## 0.4.36

- Suppress stale backup-gate errors on the main page once a fresh Home Assistant backup is visible.
- Reset empty persisted error states on startup instead of showing an unexplained error badge.

## 0.4.35

- Split action buttons into HA-to-Git and Git-to-HA rows, with Save and Apply as primary actions.
- Accept recent Home Assistant automatic backups stored in a local or configured location when enforcing the fresh backup gate.

## 0.4.34

- Display Last Run and preview generation timestamps in the Home Assistant local timezone.
- Render Save Preview diffs with the same colored line and inline-change highlighting as conflict diffs.

## 0.4.33

- Clear transient Last Run Details, Apply Preview, and Save Preview content when the UI is refreshed or left.
- Preserve apply safety state such as preview fingerprints and storage approvals while clearing only displayed text.

## 0.4.32

- Show full Git-to-HA previews for allowlisted .storage files, including protected registries.
- Require explicit approval before applying any Git-to-HA .storage change.
- Add one-click approval for Save HA to Git conflicts where live Home Assistant should overwrite Git.

## 0.4.31

- Restore the Home Assistant live config path to /homeassistant when addon_config is also mounted at /config.

## 0.4.30

- Use the Home Assistant add-on config mount at /config as the default live config path.

## 0.4.29

- Skip managed core.config_entries projections when the live raw .storage file is missing.
- Show interrupted startup actions as interrupted instead of error.

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
