# Archived: Home Assistant Area-Split Organizer

Status: retired.

HA Ops once explored a Git projection that split Home Assistant automations,
scripts, and scenes into `.ha-ops/areas/<area>/` files. It was never a Home
Assistant include format: the normal heap files remained the live format.

The projection, its proposed routing and precedence rules, its tests, and its
backup implementation were retired in HA Ops 0.11.1. Do not enable, restore,
or extend this format. HA Ops uses the normal `automations.yaml`,
`scripts.yaml`, and `scenes.yaml` heap files instead.

Historical implementation and design material remain available in Git history.
The small runtime guard remains deliberately: a legacy `organizer: enabled`
setting is rejected before any Save or Apply work can change configuration.
