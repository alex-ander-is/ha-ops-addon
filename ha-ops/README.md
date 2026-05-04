# HA Ops

HA Ops manages a Git-backed Home Assistant config from an ingress UI.

## Actions

- `Preview Apply`: fetch `repo_branch`, read `ha-ops.json`, and show the diff that Apply would make.
- `Pull & Apply`: fetch `repo_branch`, read `ha-ops.json`, snapshot live targets, then apply Git config to Home Assistant.
- `Export`: recreate local `export` from `origin/<repo_branch>` and copy live config into it.
- `Push`: commit local export changes and push them to `origin/export`.
- `Rollback`: restore a saved local release snapshot.

## Source of truth

- `main` is the source of truth for Apply.
- `export` is a review branch for bootstrapping or refreshing config from the live Home Assistant instance.
- HA Ops never merges `export` into `main`; review and merge are done in Git.

## Export policy

Home Assistant export is config-only.

Exported:

- root `*.yaml` and `*.yml`, except `secrets.yaml`
- `blueprints/`
- `custom_templates/`
- `dashboards/`
- `packages/`
- `templates/`
- `themes/`
- `ui_lovelace_minimalist/`
- selected safe `.storage` config files
- selected Zigbee2MQTT config paths under `zigbee2mqtt/`

Skipped:

- `secrets.yaml`
- auth, session, and token `.storage` files
- databases and logs
- cache, backups, deps, tts, media
- downloaded `custom_components`
- frontend assets and `www`
- binaries and generated runtime files

## Apply policy

- Home Assistant config is synced from Git into `/homeassistant`.
- Selected `.storage` files are applied as an overlay.
- Unmanaged auth, session, token, secret, database, log, cache, downloaded integration, frontend, and runtime files are left intact.
- If selected `.storage` files are present, Home Assistant Core is stopped before sync.
- Optional add-on targets whose source folder only contains `.gitkeep` are skipped.

## Add-on options

- `repo_url`: Git URL of the private config repository.
- `repo_branch`: branch to apply from, usually `main`.
- `repo_path`: local checkout directory inside `/data`.
- `manifest_path`: path to `ha-ops.json` inside the Git repository.
- `apply_path`: fallback Home Assistant source path.
- `git_ssh_key`: optional private deploy key.
- `create_release_snapshot`: save local release snapshots before Apply.
- `create_ha_backup`: create Home Assistant partial backups before Apply.
- `ha_backup_name_prefix`: prefix for generated Home Assistant backup names.
- `restart_after_apply`: default restart behavior for manifest targets.

## Deploy key

For a private GitHub repository:

1. Set `repo_url` to the SSH URL.
2. Open HA Ops.
3. Click `Generate Deploy Key`.
4. Add the shown public key to GitHub Deploy Keys.
5. Leave `git_ssh_key` empty to use the generated key.

## Expected repository layout

```text
ha-config/
  ha-ops.json
  homeassistant/
  addons/
```
