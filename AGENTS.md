# Agent Instructions

All docs and interface must be written in US English.
For `ha-ops-addon`, every block/bunch of changes must bump `ha-ops/config.yaml` version and add a matching `ha-ops/CHANGELOG.md` entry before last of commits. If the work involves series of commits, bump should be performed at the end. The commit that bumps `ha-ops/config.yaml` must also create a Git tag with exactly the same version, for example version `0.6.9` must have tag `0.6.9`. This is required because Home Assistant add-on updates depend on the version field.
