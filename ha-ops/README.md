# HA Ops

`ha-ops` is a custom Home Assistant add-on that treats a Git repository as the source of truth for `/config`.

## Current behavior

1. Clone or update a private `ha-config` repository into the add-on data directory.
2. Read `ha-ops.json` from that repository to determine which targets are managed.
3. Optionally create a Home Assistant partial backup for the managed targets.
4. Snapshot all managed live targets into `/data/releases/<timestamp>`.
5. Sync each target from Git into its live path.
6. Stop add-ons before sync when their live data should not be updated hot, then start them again after the sync.
7. Restart managed add-ons like Mosquitto and Zigbee2MQTT when their config changes.
8. Stop or restart Home Assistant Core at the correct step when `/config/.storage` is part of the apply.
9. Export current live target config back into the local Git checkout on the `export` branch for bootstrap.
10. Commit and push exported local Git changes to `origin/export` from the ingress UI.
11. Allow rollback to any saved local release from the ingress UI.

## Add-on options

- `repo_url`: Git URL of the private config repository.
- `repo_branch`: Branch to apply from.
- `repo_path`: Local checkout directory inside `/data`.
- `manifest_path`: Path to the deployment manifest inside the Git repository.
- `apply_path`: Path inside the Git repo that should be mirrored into `/config`.
- `git_ssh_key`: Optional deploy key for `git@github.com:...` style URLs.
- `create_release_snapshot`: Save `/config` into `/data/releases/<timestamp>` before every apply.
- `create_ha_backup`: Create a Home Assistant partial backup before every apply.
- `ha_backup_name_prefix`: Prefix used for generated Home Assistant backup names.
- `restart_after_apply`: Default restart behavior for targets that do not define `restart_after_sync` in the deployment manifest.

## GitHub deploy key

For a private GitHub repository, prefer an SSH deploy key:

1. Set `repo_url` to `git@github.com:alex-ander-is/ha-config.git`.
2. Open the add-on ingress page and click `Generate Deploy Key`.
3. Copy the displayed public key into GitHub Deploy Keys for `ha-config`.
4. Leave `git_ssh_key` empty if you want HA Ops to use its generated key automatically.

If you prefer to manage the private key yourself, you can still paste it into `git_ssh_key`.

## Expected `ha-config` layout

```text
ha-config/
  ha-ops.json
  homeassistant/
    configuration.yaml
    automations.yaml
    .storage/
  addons/
    mosquitto/
    zigbee2mqtt/
```

## Notes

- The add-on is intentionally one-way: Git is the source of truth, `/config` is the deployment target.
- Export and Push are explicit bootstrap actions on the `export` branch. They are not part of normal apply.
- Export skips runtime storage, cache, database, log, backup, deps, frontend bundle, media cache, and tts files by default.
- Add-on configs are accessed through `/addon_configs`, which means Mosquitto and Zigbee2MQTT can be managed in the same flow.
- The deployment manifest keeps path and restart rules in the private repo instead of hardcoding them into the add-on.
- If `.storage` is present in the source tree, Home Assistant Core is stopped before the sync to avoid live-state overwrite races.
- Zigbee2MQTT can be configured with `stop_addon_before_sync` so its `database.db`, `state.json`, and coordinator backups are not updated while the add-on is live.
