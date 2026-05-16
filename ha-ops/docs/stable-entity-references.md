# Stable Home Assistant Entity References

Status: agent playbook for edits to Home Assistant automation, script, and
scene YAML in Git.

## Goal

Make UI-managed automations, scripts, and scenes survive physical device
replacement when the replacement is given the same Home Assistant/Zigbee2MQTT
name.

The Git view must not contain Home Assistant registry UUIDs:

- no `device_id: <hex registry id>`
- no opaque `entity_id: <hex registry id>`

Prefer stable `entity_id` values derived from the device name, for example:

```yaml
entity_id: binary_sensor.living_room_smoke_smoke
```

Do not write display/friendly names into `entity_id`. This is invalid:

```yaml
entity_id: Living Room Smoke / Smoke
```

Concrete example:

Before:

```yaml
alias: living_room_smoke_detected
triggers:
  - type: smoke
    device_id: a534a78722f26cbd6d566ad9ac76c09b
    entity_id: 4de6e617bbc328a0d5888158d1d459d3
    domain: binary_sensor
    trigger: device
actions:
  - action: notify.alex
```

After:

```yaml
alias: living_room_smoke_detected
triggers:
  - trigger: state
    entity_id: binary_sensor.living_room_smoke_smoke
    to: "on"
actions:
  - action: notify.alex
```

The value `4de6e617bbc328a0d5888158d1d459d3` is the opaque Home Assistant entity
registry id. The value `binary_sensor.living_room_smoke_smoke` is the real
stable entity id to keep in Git.

## User Prompt

Suggested request:

```text
In ha-config, convert Home Assistant automations/scripts/scenes to stable
entity-based references: remove device_id usage, replace opaque registry
entity_id values with real entity_id values, and convert device triggers/actions
to entity, numeric_state, MQTT, or service calls. Do not edit HA Ops code.
Also report ghost entities and safe entity renames caused by replacement
suffixes like _2.
```

## Conversion Rules

Work on all Home Assistant automation, script, and scene YAML files that exist
in the Git checkout:

- split organizer view: `ha-config/homeassistant/.ha-ops/areas/*/automations.yaml`
- split organizer view: `ha-config/homeassistant/.ha-ops/areas/*/scripts.yaml`
- split organizer view: `ha-config/homeassistant/.ha-ops/areas/*/scenes.yaml`
- heap files, when present: `ha-config/homeassistant/automations.yaml`
- heap files, when present: `ha-config/homeassistant/scripts.yaml`
- heap files, when present: `ha-config/homeassistant/scenes.yaml`

Do not assume both layouts are present. If `.ha-ops/areas` exists and heap
files are absent, that is normal for organizer-enabled Git.

Organizer service buckets are dot-prefixed:

- `.unknown` for items with no route
- `.mixed` for items with equally plausible area routes

The only service buckets are `.unknown` and `.mixed`. Plain `unknown` can be a
real Home Assistant area directory; `_mixed` is not a valid service bucket.

Use these registries as lookup sources:

- `homeassistant/.storage/core.entity_registry`
- `homeassistant/.storage/core.device_registry`

Rules:

1. Replace opaque entity registry ids with real `entity_id`.
   Example: `4de6...` -> `binary_sensor.living_room_smoke_smoke`.
2. Convert device binary sensor triggers to state triggers.
   Example: smoke/opened/occupied -> `to: "on"`, not_opened/not_occupied -> `to: "off"`.
3. Convert device numeric sensor triggers to `numeric_state`.
   Preserve `above` and `below`.
4. Convert device conditions to `state` or `numeric_state`.
5. Convert device actions to service actions.
   Example: switch turn_on -> `action: switch.turn_on` with `target.entity_id`.
6. Convert Zigbee2MQTT button/device action triggers to MQTT triggers.
   Topic is `zigbee2mqtt/<device-name-without-area-icon>/action`.
   Payload is the old `subtype`.
7. Leave no `device_id` in managed automation, script, or scene YAML.
8. Leave no opaque hex `entity_id` in managed automation, script, or scene YAML.

Before finishing, run:

```sh
# from ha-config/homeassistant
managed_paths=()
for path in automations.yaml scripts.yaml scenes.yaml \
  .ha-ops/areas/*/automations.yaml .ha-ops/areas/*/scripts.yaml .ha-ops/areas/*/scenes.yaml \
  .ha-ops/areas/.*/automations.yaml .ha-ops/areas/.*/scripts.yaml .ha-ops/areas/.*/scenes.yaml; do
  [ -e "$path" ] && managed_paths+=("$path")
done
rg --hidden -n "\\bdevice_id:" "${managed_paths[@]}"
rg --hidden -n "entity_id: [0-9a-f]{16,}|- [0-9a-f]{16,}" "${managed_paths[@]}"
python3 - <<'PY'
from pathlib import Path
import yaml
paths = [
    *[Path(name) for name in ("automations.yaml", "scripts.yaml", "scenes.yaml") if Path(name).exists()],
    *sorted(Path(".ha-ops/areas").glob("*/*.yaml")),
    *sorted(Path(".ha-ops/areas").glob(".*/*.yaml")),
]
for path in paths:
    yaml.safe_load(path.read_text())
print("yaml ok")
PY
```

## Ghost Entities

Ghost entities are registry entries left from replaced devices. They often cause
new replacement entities to get suffixes such as `_2`.

A candidate ghost entity usually has:

- an old `device_id` whose device no longer exists physically
- a stable base object id that collides with the new replacement entity
- a replacement entity with the same base plus `_2`
- no current use in managed automation, script, or scene YAML after conversion

For safety, do not delete automatically. Produce a report with:

- old entity registry id
- old `entity_id`
- old device id and device name
- replacement `entity_id`
- reason it appears safe
- exact file references still using either entity, if any

## Safe Renames

Only propose renaming replacement entities when the unsuffixed name has no live
collision after the ghost is removed.

Example:

- ghost: `sensor.bathroom_presence_illuminance`
- replacement: `sensor.bathroom_presence_illuminance_2`
- proposed rename: `sensor.bathroom_presence_illuminance_2` ->
  `sensor.bathroom_presence_illuminance`

Do not rename automatically unless explicitly asked. A safe rename plan must
include the files that need updating after the Home Assistant registry rename.

## Non-goals

- Do not edit HA Ops implementation unless the user explicitly asks for a
  product feature.
- Do not store display names as `entity_id`.
- Do not keep `device_id` just because Home Assistant UI generated it.
