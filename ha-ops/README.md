# HA Ops

HA Ops manages Home Assistant config with Git-backed previews and service branches.

## Actions

- `Preview HA to Git`: export live Home Assistant config, update service branches, and show the merge result before Save.
- `Save HA to Git`: commit selected preview files from the confirmed HA to Git merge into `repo_branch` and push.
- `Preview Git to HA`: export current live Home Assistant config to `ha-ops/ha-live`, push service branches, and show the merge diff before Apply.
- `Apply Git to HA`: apply selected preview files after matching preview and safety checks.
- `Rollback`: restore a saved local release snapshot.

## Repository Model

- `repo_branch`, usually `main`, is the user-managed branch.
- `ha-ops/ha-live` stores the latest exported live Home Assistant config used as a merge branch.
- `ha-ops/base` stores the common base between Git and the latest live export.
- The repository may be empty before first use.
- `ha-ops.json` is optional; when it is missing, HA Ops uses a built-in default manifest.
- There is no user-facing `export` branch.

## Organizer Contract

HA Ops has an opt-in contract for a virtual split of Home Assistant UI-managed
automations, scripts, and scenes. Live Home Assistant keeps the normal heap
files. Keep organizer disabled for production Save and Apply flows; disabled
targets use the heap files directly and remove stale generated `.ha-ops/areas`
metadata. Enabled `.ha-ops/areas` projection remains blocked pending a
production-safe rewrite. See `docs/organizer-contract.md` for the paused
contract and safety invariants.

## Stable Entity References

When editing Home Assistant automations, scripts, and scenes in Git, keep them
independent of Home Assistant registry UUIDs. This applies to both the future
organizer split view under `homeassistant/.ha-ops/areas/<area>/` and the heap files
`homeassistant/automations.yaml`, `homeassistant/scripts.yaml`, and
`homeassistant/scenes.yaml` when those files are present. Convert opaque
`entity_id` registry ids to real stable `entity_id` values, remove `device_id`
usage, and prefer state, numeric state, MQTT, or service actions. See
`docs/stable-entity-references.md`.

Future organizer service buckets are dot-prefixed: `.unknown` for unrouted
items and `.mixed` for equally plausible area routes. Real areas, including an
area named `Unknown`, use normal non-dot directory names.

Suggested request for agents:

```text
In ha-config, convert Home Assistant automations/scripts/scenes to stable
entity-based references: remove device_id usage, replace opaque registry
entity_id values with real entity_id values, and convert device triggers/actions
to entity, numeric_state, MQTT, or service calls. Do not edit HA Ops code.
Also report ghost entities and safe entity renames caused by replacement
suffixes like _2.
```

Example conversion:

```yaml
# before
- type: smoke
  device_id: a534a78722f26cbd6d566ad9ac76c09b
  entity_id: 4de6e617bbc328a0d5888158d1d459d3
  domain: binary_sensor
  trigger: device

# after
- trigger: state
  entity_id: binary_sensor.living_room_smoke_smoke
  to: "on"
```

## Save Policy

Home Assistant is the source of truth for `Save HA to Git`. Export is config-only:
HA Ops writes allowlisted live config, including allowlisted `.storage`, to Git.

After a preview, the Change List starts with no files selected. Check the files
you want to process, or use `Select All`; unchecked files stay unchanged in Git.
For selected non-conflict files, Save defaults to the HA version unless you
choose `Keep Unchanged`.

On the first HA-to-Git preview for a repository without Home Assistant config,
HA Ops also offers `.editorconfig`, `.vscode/settings.json`, and
`.prettierignore` as optional Git-only editor and formatter settings. They
preserve the existing end-of-file formatting of each file and are never written
to live Home Assistant. Select them to keep them in Git, or leave them
unchecked to omit them.

Saved:

- root `*.yaml` and `*.yml`, except `secrets.yaml`
- `blueprints/`
- `custom_templates/`
- `dashboards/`
- `packages/`
- `templates/`
- `themes/`
- `ui_lovelace_minimalist/`
- selected allowlisted `.storage` config files, including protected registry and instance files
- safe managed projections such as `.storage_managed/core.config_entries.json`
- selected App config folders

Preserved:

- Git-only files outside the Home Assistant managed export paths, for example docs or README files inside `homeassistant/`

Note:

- Save exports the full `.storage` allowlist. Protected files such as device and entity registries and `person` are saved to Git by design.
- Sensitive raw `.storage` files such as auth, sessions, tokens, secrets, and raw `core.config_entries` are not saved. `core.config_entries` is represented only by the managed projection.
- If fresh HA config conflicts with the Git checkout and there is no trusted common base, HA Ops stops, shows a per-file diff, and lets the user choose HA or Git for each selected conflicted file.

Skipped:

- `secrets.yaml`
- auth, session, and token `.storage` files
- databases and logs
- cache, backups, deps, tts, media
- downloaded `custom_components`
- frontend assets and `www`
- binaries and generated runtime files

## Apply Policy

- Git config is applied as an overlay, not as a destructive mirror.
- Missing files in Git do not delete live Home Assistant files during normal overlay apply. In conflict previews, a selected Git-side deletion can remove the matching live file.
- Selected App config is applied as an overlay by default.
- Selected App runtime files such as databases and logs are ignored on apply, even when present in Git.
- Empty Git source is a no-op.
- Home Assistant directories that exist in Git are applied as overlays.
- `Preview Git to HA` always shows the full diff for allowlisted `.storage`, including protected registry and instance files.
- After a preview, the Change List starts with no files selected. Check the files you want to process, or use `Select All`; unchecked files stay unchanged in live HA.
- For selected non-conflict files, Apply defaults to the Git version unless you choose `Keep Unchanged`.
- The preview decision plus `Confirm Apply to HA` is the explicit approval for selected `.storage` changes. Protected allowlisted `.storage` files are written from Git to HA only through this matching preview decision flow.
- Unmanaged auth, session, token, secret, database, log, cache, downloaded integration, frontend, and runtime files are left intact.
- Apply requires a fresh system backup visible in Home Assistant Backups and stored in a configured backup location by default.
- Apply must match the last `Preview Git to HA` commit and diff fingerprint.
- Local release snapshots are pruned by configured count and age.
- A change resolver classifies Home Assistant changes as YAML and/or `.storage`; lifecycle actions are controlled by explicit flags.
- YAML-only apply reloads Home Assistant config by default instead of restarting Core.
- Theme-only apply reloads frontend themes with `frontend.reload_themes` instead of restarting Core or running the general YAML reload.
- Lovelace resource storage (`lovelace_resources`) apply reloads Lovelace resources by default instead of restarting Core, with restart fallback if the soft reload fails.
- Dashboard/config `.storage` files such as `lovelace.lovelace`, `lovelace_dashboards`, and `lovelace.map` use the normal storage lifecycle because `lovelace.reload_resources` is resources-only.
- Other `.storage` apply or rollback can stop/start Core by policy because Home Assistant may otherwise keep stale state or rewrite those files.

## Managed Apps

- HA Ops discovers installed Apps through Supervisor.
- Apps are unmanaged by default.
- Check an App in the UI to include its config in `Save HA to Git` and future Git-to-HA apply.
- Uncheck an App in the UI to exclude it, even when `ha-ops.json` exists.
- Set `delete: true` in an optional manifest only when intentionally mirroring an App folder destructively.
- Zigbee2MQTT is detected from installed App metadata instead of a hard-coded slug.
- If Zigbee2MQTT stores config under `/config/zigbee2mqtt`, HA Ops can use that existing path instead of assuming `/addon_configs/<slug>`.

## Disk Usage

- `Check disk usage` is read-only and prints a storage summary to the Log.
- The report starts with a single Storage tree: total used storage, inferred System storage, visible App data, visible Home Assistant config, and Free space.
- The Home Assistant branch keeps useful child details such as the database, Zigbee2MQTT, `custom_components`, `.storage`, `www`, and logs when those paths are visible.
- Filesystem rows and top-level totals are deduplicated by backing filesystem totals so repeated mapped paths on the same device are counted once.
- System storage is inferred from filesystem used space minus paths the App can read. If HA Ops cannot account for host-only paths, the report marks that part as partial or unavailable instead of pretending it inspected the host.
- The Docker API capability is broad. HA Ops checks its current Supervisor grants before Docker diagnostics or cleanup, then negotiates Docker Engine API 1.39 through 1.52 and uses a versioned `/system/df` and versioned prune endpoint.
- If Protection mode is enabled, Docker diagnostics and `Clear build cache` are unavailable. An administrator must turn off Protection mode for the installed HA Ops App, restart HA Ops, and refresh the page. Updating HA Ops does not change that per-install security setting.
- The build-cache estimate is an upper bound at the time of the check. Shared records can be selected while their bytes remain referenced, so the cleanup may free less space.
- `Clear build cache` makes one irreversible `all=true` request after a delayed second confirmation. It removes all build-cache types that Docker considers unused, cannot restore cache, and can make later builds slower. It does not prune images, containers, volumes, or networks.
- An interrupted or uncertain cleanup remains durably fenced across refresh, restart, and App updates. HA Ops reconciles an orphaned same-process worker to an acknowledgement screen; acknowledgement does not retry or restore cache, and corrupted fence state also requires explicit acknowledgement.
- Path traversal and optional diagnostics are bounded. If Supervisor, Docker, journal, or filesystem diagnostics time out or are unavailable, the report marks that section as unavailable and keeps the rest of the summary.

## App Options

- `repo_url`: Git URL of the private config repository.
- `repo_branch`: branch to save and apply, usually `main`.
- `repo_path`: local checkout directory inside `/data`.
- `manifest_path`: optional manifest path inside the repository.
- `apply_path`: fallback Home Assistant source path.
- `git_ssh_key`: optional private deploy key.
- `create_release_snapshot`: save local release snapshots before Apply.
- `create_ha_backup`: create a full Home Assistant system backup when no fresh backup is available before Apply.
- `ha_backup_name_prefix`: prefix for generated Home Assistant backup names.
- `require_fresh_backup`: require a fresh system backup before Apply.
- `backup_max_age_hours`: maximum age for the latest system backup, default `24`.
- `backup_require_location`: require the fresh system backup to be stored in a configured location, default `true`.
- `max_apply_deletions`: maximum number of previewed file deletions allowed before Apply.
- `release_snapshot_keep_count`: maximum local release snapshots to keep, default `5`.
- `release_snapshot_keep_days`: maximum local release snapshot age in days, default `7`.
- `reload_yaml_after_apply`: reload Home Assistant YAML after YAML changes, default `true`.
- `restart_core_after_apply`: restart Home Assistant Core after Apply, default `false`.
- `stop_core_before_storage_apply`: stop Home Assistant Core before applying `.storage`, default `true`.
- `start_core_after_storage_apply`: start Home Assistant Core after HA Ops stopped it for Apply, default `true`.
- `reload_yaml_after_rollback`: reload Home Assistant YAML after rollback YAML changes, default `false`.
- `restart_core_after_rollback`: restart Home Assistant Core after rollback, default `false`.
- `stop_core_before_storage_rollback`: stop Home Assistant Core before rolling back `.storage`, default `true`.
- `start_core_after_storage_rollback`: start Home Assistant Core after HA Ops stopped it for rollback, default `true`.
- `restart_after_apply`: legacy alias for `restart_core_after_apply` when explicit lifecycle flags are not set.

## Deploy Key

For a private GitHub repository:

1. Set `repo_url` to the SSH URL.
2. Open HA Ops.
3. Click `Generate Deploy Key`.
4. Add the shown public key to GitHub Deploy Keys.
5. Leave `git_ssh_key` empty to use the generated key.

Raw `core.config_entries` is not applied from Git. HA Ops applies only the safe managed projection for supported fields.

## Local Web Harness

Run the safe local web interface without installing HA Ops into Home Assistant:

```bash
python3 ha-ops/dev_harness.py --port 8099 --keep-root
```

The harness binds only to `127.0.0.1` and prints an ingress-like URL such as
`http://127.0.0.1:8099/api/hassio_ingress/local-ha-ops/`. It creates disposable
`data/`, `homeassistant/`, `addon_configs/`, a fake Git remote, and a fake
Supervisor. Preview jobs are synthetic deterministic jobs with test-only gates,
so browser tests can hold `running`, check reconnect/replay, then release the
job without touching live Home Assistant.

Automated browser smoke:

```bash
PLAYWRIGHT_SHARED_ROOT=/Users/purportex/Applications/Playwright node ha-ops/tests/browser/run.mjs
```

Covered locally: Lit/Vaadin page load, Preview Git to HA, Preview HA to Git,
WebSocket connect/reconnect and revision replay, lazy per-file `diff-get`, debug
snapshot redaction, disabled/running controls, mobile layout, and HTTP dispatch
when WebSocket is known to be unavailable before a command is sent.

Not covered locally: real Supervisor ingress proxying, live backups, Core
restart/reload, App lifecycle actions, Docker socket effects, and writes to the
real HA config or user Git remotes.

The UI requires JavaScript. Its HTTP fallback is a JavaScript command-transport
fallback used only when WebSocket is known to be unavailable; there is no
server-rendered/no-JavaScript preview or POST control layer. The server remains
authoritative for state, selection, command status, and mutations; the browser
does not optimistically apply command results.
