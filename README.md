# ha-ops-addon

Public Home Assistant add-on repository for operating a Git-backed HA configuration.

The first add-on in this repository is `ha-ops`, an ingress UI that:

- fetches `ha-config` from Git
- snapshots managed live targets
- applies Home Assistant, Mosquitto, and Zigbee2MQTT config from Git
- creates an optional HA partial backup before apply
- supports rollback from saved local releases

See [`ha-ops/README.md`](./ha-ops/README.md) for setup details.

## Install

For development, copy or clone this repository into the Home Assistant add-ons directory on HAOS:

```text
/addons/ha-ops-addon
```

Then add that local repository in Home Assistant and install the `HA Ops` add-on.

For long-term use, publish this repository on GitHub and add its URL in the Home Assistant add-on store.
