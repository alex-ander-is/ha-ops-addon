# Changelog

Released sections are immutable. Put every new change into a new version section.

## 0.8.34

- Make partial Git-to-HA Apply converge for organizer-backed Home Assistant automations and scripts by materializing selected area YAML changes into the live heap files while leaving unselected organizer and storage diffs pending.

## 0.8.33

- Refresh the Git-to-HA Apply Change List after a successful partial Apply so applied files disappear, unselected files remain visible, and the refreshed preview commit tracks the repo branch.

## 0.8.32

- Add inline help under the highlighted post-apply HA-to-Git review button so users understand it is still the normal preview action.

## 0.8.31

- Rework Disk Usage into a single read-only Storage tree with inferred System, App data, Home Assistant, and Free space categories.
- Deduplicate repeated filesystem rows by backing filesystem totals and keep Home Assistant config child details in the report.
- Keep filesystem, Docker, Supervisor, journal, and path traversal diagnostics bounded with explicit partial or unavailable messages.

## 0.8.30

- Add a read-only Disk Usage action that prints mapped HA Ops storage sizes, Supervisor host disk fields, and Docker `/system/df` diagnostics to the Log.
- Declare `docker_api: true` for read-only Docker `/system/df` diagnostics; this Home Assistant add-on capability is broad.

## 0.8.29

- Complete required Home Assistant entity registry metadata before Git-to-HA Apply writes reduced registry data.
- Roll back Home Assistant config files and managed storage projections when Apply or config check fails.
- Keep HA Ops service branches aligned with live normalized storage after successful Apply.

## 0.8.28

- Keep Git-to-HA Apply writes on raw previews so registry diff normalization cannot remove Home Assistant metadata.
- Reject invalid or incomplete protected registry storage before stopping Home Assistant Core.
- Try to start Home Assistant Core if Apply fails after a storage sync stop.

## 0.8.27

- Remove the duplicate textual preview summary above the interactive Change List.
- Keep unmanaged organizer area files out of Git-to-HA previews and apply trees.

## 0.8.26

- Show stale HA Ops internal Git branch push failures as warnings with Reset Git State guidance.
- Keep Apply preview organizer diffs anchored to the Git organized YAML so added files keep their area path and block scalar formatting.

## 0.8.25

- Keep the Log scrolled to the newest line by default while preserving manual scrollback.

## 0.8.24

- Refresh the Save Preview Change List after a successful Save so committed files disappear immediately.

## 0.8.23

- Add a Reset Git State recovery action that rebuilds HA Ops service branches from current main and live Home Assistant export.

## 0.8.22

- Align expanded preview Collapse Diff controls left while keeping the preview choice toggle on the right.
- Scroll to the next preview file after collapsing an expanded diff.
- Keep unicode escape hover spans intact when inline diff highlighting cuts through an escape code.
- Treat stale Save previews that rebuild to no changes as a successful no-op instead of asking for review.
- Keep unselected HA-to-Git Save files visible in later previews after a partial Save.

## 0.8.21

- Explain disabled preview Confirm actions when no Change List files are selected.
- Update README preview selection, storage approval, and Git deletion behavior.

## 0.8.20

- Keep expanded preview files open after changing per-file preview choices.
- Add explicit per-file preview selection so Save and Apply only process checked Change List rows.
- Keep cleanly merged conflict-preview files under the same Change List selection rules.

## 0.8.19

- Add a lower Collapse Diff button to expanded per-file preview diffs.
- Keep organizer YAML dumps from writing explicit null values.
- Show rendered characters when hovering over Unicode escape codes in diffs.

## 0.8.18

- Keep the Managed Targets checkbox header from overlapping the Target column.
- Repair stale running UI state when no HA Ops job is active.

## 0.8.17

- Show per-file preview choices as visible toggles, moving them below the diff when a file is expanded, and keep Confirm as the only Save or Apply trigger.

## 0.8.16

- Move per-file preview choice actions below expanded diffs and keep the wrap control in the file header.

## 0.8.15

- Use the Home Assistant YAML dumper for organizer output instead of custom Jinja scalar formatting.

## 0.8.14

- Normalize Home Assistant organizer Jinja YAML scalars to avoid quote-only Save preview diffs.

## 0.8.13

- Group Save and Apply preview diffs by file with collapsed per-file rows, change labels, global expand/collapse controls, and footer Confirm/Cancel actions.

## 0.8.12

- Hide stale Save and Apply previews while deleted_devices cleanup is waiting for confirm or revert.

## 0.8.11

- Remove the obsolete Apply storage approval endpoint and keep early Save errors from escaping the job error handler.

## 0.8.10

- Preserve Save merge commits when retrying a failed push after the Git branch moved.

## 0.8.9

- Apply clean Git-only deletions shown in conflict previews to live Home Assistant and count them against Apply deletion limits.

## 0.8.8

- Preserve same-content divergent service-branch merges as real merge commits.

## 0.8.7

- Default unresolved non-conflict Apply preview paths to Git when another path is explicitly kept from HA.

## 0.8.6

- Document the HA Ops 0.8 service-branch merge contract for future code reviews.
- Show and fingerprint cleanly merged files in conflict previews.

## 0.8.5

- Require explicit HA/Git choices for every conflict preview file before Save or Apply can be confirmed.

## 0.8.4

- Refresh local HA Ops service branches from origin before rebuilding live previews.
- Update README and job details for service-branch previews.

## 0.8.3

- Reject stale HA to Git conflict previews when live Home Assistant content changes after preview.
- Resolve modify/delete merge conflicts when the selected side deleted the file.

## 0.8.2

- Reject stale Git to HA conflict previews when live Home Assistant changes after preview.
- Apply confirmed protected .storage merge conflicts instead of reporting success after skipping them.

## 0.8.1

- Bootstrap HA Ops service branches for existing repositories that only have the configured Git branch.
- Keep freshly exported live HA state when building Git to HA merge previews, allow conflict previews to be confirmed, and clean up no-op merges.

## 0.8.0

- Rework HA to Git and Git to HA previews around HA Ops service branches so both directions use Git-style three-way merges.
- Require a fresh Preview before Save or Apply confirmation, and warn with an updated preview when Git or live HA changed in between.

## 0.7.29

- Move HA to Git and Git to HA approvals into the preview step with all-file and per-file HA/Git choices.

## 0.7.28

- Show Git to HA registry safety warnings as a separate Apply Preview warning panel instead of injecting them into the diff.

## 0.7.27

- Show Git to HA registry safety warnings directly in the preview diff and warn when live registry devices or entities would be removed.

## 0.7.26

- Warn when Git to HA would downgrade newer live Home Assistant entity registry metadata.

## 0.7.25

- Reject HA Ops pushes that forget to include a version bump, changelog entry, and matching release tag.

## 0.7.24

- Keep HA Ops organizer from parsing Home Assistant time strings like `21:00:00` as sexagesimal integers.

## 0.7.23

- Highlight post-apply HA registry follow-up with a persistent warning and orange HA to Git preview button.
- Preserve organizer contract docs during HA to Git previews and saves.

## 0.7.22

- Keep the Managed Targets checkbox column fixed to checkbox width.

## 0.7.21

- Keep long Home Assistant organizer YAML scalars on one line to avoid noisy Save HA to Git diffs.

## 0.7.20

- Request Home Assistant Core API access so Git to HA can reload YAML after applying changes.

## 0.7.19

- Rename the Actions IDs migration button to make the Git write explicit.

## 0.7.18

- Use live Zigbee2MQTT data as read-only context for Actions IDs migrations when runtime files are ignored by Git.

## 0.7.17

- Use selected Zigbee2MQTT add-on source data when previewing and applying Actions IDs migrations.

## 0.7.16

- Clear all stale preview panels when starting any preview or maintenance check.

## 0.7.15

- Treat Home Assistant device registry sw_version as redundant runtime data in previews, saves, and applies.

## 0.7.14

- Keep the retained devices delete checkbox column fixed to checkbox width.

## 0.7.13

- Read retained devices MQTT credentials from the Supervisor MQTT service instead of a Mosquitto container file.
- Declare the HA Ops MQTT service dependency so retained devices checks can authenticate to the broker.

## 0.7.12

- Add spacing below the Log header.
- Use direct Mosquitto clients for retained devices checks instead of Docker.

## 0.7.11

- Render Log entries by appending details first and the latest message last.

## 0.7.10

- Split maintenance checks into separate UI sections with explicit flow descriptions.

## 0.7.9

- Avoid duplicate log lines when a running action message is also the latest detail.
- Skip Internal IDs migrations for Zigbee2MQTT registry devices missing from current Zigbee2MQTT files.

## 0.7.8

- Report dirty checkout paths before Git sync actions instead of showing a raw pull rebase error.
- Accept both repository-root and apply-path Internal IDs migration changes when auto-committing pending migrations.

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
