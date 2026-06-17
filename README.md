# ha-ops-addon

Home Assistant add-on repository for HA Ops.

`ha-ops` is an ingress UI for managing a Git-backed Home Assistant configuration:

- Preview live Home Assistant config before saving it to Git
- Save selected Home Assistant preview changes to the configured Git branch
- Preview Git changes through service branch merges before applying them to Home Assistant
- Select exactly which preview files Save or Apply should process
- Apply Git config as an overlay, with deletions only when explicitly selected from a preview
- Discover installed add-ons and let the user choose which add-on configs are managed
- Require fresh system backups and create full Home Assistant backups when needed
- Create pruned local release snapshots
- Roll back from saved local releases

See [`ha-ops/README.md`](./ha-ops/README.md) for setup and behavior.

## Install

Add this repository URL in the Home Assistant add-on store, then install `HA Ops`.

## Presentation Assets

- `ha-ops/icon.png` is the Home Assistant app/add-on list icon.
- `ha-ops/logo.png` is the add-on presentation logo.
- Home Assistant update indicators come from Supervisor version state, not from these image files.

For local HAOS development, clone this repository into:

```text
/addons/ha-ops-addon
```
