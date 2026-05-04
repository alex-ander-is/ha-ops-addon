# ha-ops-addon

Home Assistant add-on repository for HA Ops.

`ha-ops` is an ingress UI for managing a Git-backed Home Assistant configuration:

- Preview Apply and Pull & Apply from `ha-config/main`
- Export live config into a local `export` branch
- Push `export` to `origin/export` for review
- create release snapshots and optional Home Assistant backups
- roll back from saved local releases

See [`ha-ops/README.md`](./ha-ops/README.md) for setup and behavior.

## Install

Add this repository URL in the Home Assistant add-on store, then install `HA Ops`.

For local HAOS development, clone this repository into:

```text
/addons/ha-ops-addon
```
