# HA Ops

HA Ops manages Home Assistant config with a single Git branch.

## Actions

- `Save HA to Git`: export live Home Assistant config into `repo_branch`, commit, and push.
- `Preview Git to HA`: fetch `repo_branch` and show the diff that would be applied to live Home Assistant.
- `Apply Git to HA`: apply Git config after matching preview and safety checks.
- `Rollback`: restore a saved local release snapshot.

## Repository Model

- `repo_branch`, usually `main`, is the only normal branch.
- The repository may be empty before first use.
- `ha-ops.json` is optional; when it is missing, HA Ops uses a built-in default manifest.
- There is no user-facing `export` branch.

## Save Policy

Home Assistant export is config-only.

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
- selected add-on config folders

Preserved:

- Git-only files outside the Home Assistant managed export paths, for example docs or README files inside `homeassistant/`

Note:

- Save exports the full `.storage` allowlist. Protected files such as `core.config_entries`, device and entity registries, and `person` are saved to Git by design.

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
- Missing files in Git do not delete live Home Assistant files.
- Selected add-on config is applied as an overlay by default.
- Selected add-on runtime files such as databases and logs are ignored on apply, even when present in Git.
- Empty Git source is a no-op.
- Home Assistant directories that exist in Git are applied as overlays.
- Selected `.storage` files are applied as an overlay, except protected files unless `allow_protected_storage` is explicitly set.
- Unmanaged auth, session, token, secret, database, log, cache, downloaded integration, frontend, and runtime files are left intact.
- Apply requires a fresh system backup visible in Home Assistant Backups and stored in a configured backup location by default.
- Apply must match the last `Preview Git to HA` commit and diff fingerprint.
- Local release snapshots are pruned by configured count and age.

## Managed Add-ons

- HA Ops discovers installed add-ons through Supervisor.
- Add-ons are unmanaged by default.
- Check an add-on in the UI to include its config in `Save HA to Git` and future Git-to-HA apply.
- Uncheck an add-on in the UI to exclude it, even when `ha-ops.json` exists.
- Set `delete: true` in an optional manifest only when intentionally mirroring an add-on folder destructively.
- Zigbee2MQTT is detected from installed add-on metadata instead of a hard-coded slug.
- If Zigbee2MQTT stores config under `/config/zigbee2mqtt`, HA Ops can use that existing path instead of assuming `/addon_configs/<slug>`.

## Add-on Options

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
- `restart_after_apply`: default restart behavior for targets that do not define their own restart rule.

## Deploy Key

For a private GitHub repository:

1. Set `repo_url` to the SSH URL.
2. Open HA Ops.
3. Click `Generate Deploy Key`.
4. Add the shown public key to GitHub Deploy Keys.
5. Leave `git_ssh_key` empty to use the generated key.

Set `allow_protected_storage: true` in an optional manifest only when intentionally applying protected `.storage` files such as `core.config_entries`.
