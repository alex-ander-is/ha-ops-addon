# Home Assistant Organizer Contract

Status: planned. This document is a contract for future implementation, not an
implemented feature.

## Goal

Home Assistant remains the source of truth for UI-managed automations, scripts,
and scenes. Home Assistant stores them as heap files:

- `homeassistant/automations.yaml`
- `homeassistant/scripts.yaml`
- `homeassistant/scenes.yaml`

HA Ops stores an intermediate, area-first Git view for humans and agents:

```text
homeassistant/.ha-ops/areas/
  home/
    automations.yaml
    scripts.yaml
    scenes.yaml
  office/
    automations.yaml
    scripts.yaml
    scenes.yaml
  unknown/
    automations.yaml
    scripts.yaml
    scenes.yaml
```

The `.ha-ops/areas` tree is HA Ops managed metadata. It is not a Home Assistant
include tree and must not be applied to live Home Assistant as ordinary config.

## Sync Model

`Save HA to Git` must:

1. Read the live Home Assistant heap files.
2. Read Home Assistant registries from `.storage`.
3. Split automations, scripts, and scenes into the `.ha-ops/areas/<area>/` Git
   view.
4. Keep or regenerate integrity metadata.
5. Preserve item payloads without semantic loss.

`Apply Git to HA` must:

1. Read the `.ha-ops/areas/<area>/` Git view.
2. Compose it back into the live heap files.
3. Write only the heap files to live Home Assistant.
4. Refuse to apply if integrity checks show loss, duplication, or malformed
   data.

## Routing Order

Routing must be independent of one specific home. The preferred source is the
area assigned in Home Assistant UI to the automation, script, or scene entity.
If that is missing, HA Ops must use deterministic fallbacks.

Required routing order:

1. Explicit organizer override, if configured.
2. UI area from `core.entity_registry` for `automation.*`, `script.*`, or
   `scene.*`.
3. Prefix rules on automation alias, script key, script alias, or scene name.
4. Direct `area_id` references inside the item.
5. Referenced `device_id` resolved through `core.device_registry`.
6. Referenced `entity_id` resolved through `core.entity_registry` and then
   optionally through `core.device_registry`.
7. `_mixed` if multiple areas are equally plausible and no deterministic owner
   exists.
8. `unknown` if no owner can be found.

The owner of an automation or script is the automation or script entity area
when UI area exists. Trigger/action devices are references, not owners.

## Identity Rules

Automations:

- Identity is `id`.
- Duplicate `id` values are an integrity error.
- Missing `id` is allowed only if Home Assistant accepts it, but the item must
  still be counted and preserved.

Scripts:

- Identity is the YAML mapping key.
- Duplicate keys are an integrity error.
- The key, not the alias, determines `script.<key>`.

Scenes:

- Identity is `id` when present, otherwise `name`.
- Duplicate scene identities are an integrity error.

## Integrity Requirements

Tests must cover both super-set fixtures and edge cases. At minimum, every split
and compose operation must verify:

- total automation count is unchanged
- total script count is unchanged
- total scene count is unchanged
- automation ids are preserved exactly
- script keys are preserved exactly
- scene identities are preserved exactly
- no item disappears into an unreferenced file
- no item is duplicated across areas
- item payloads round-trip without semantic loss
- `_mixed` and `unknown` are explicit buckets, not silent guesses
- malformed split files fail before writing live Home Assistant heap files

## Required Test Fixture Shape

The contract tests use synthetic super-set fixtures, not a private home
configuration. Fixtures must include:

- UI-area-owned automation with references to another area
- UI-area-owned script with references to another area
- automation renamed in alias while retaining stable `id`
- script whose alias differs from its YAML key
- time-only automation with UI area
- automation with explicit override
- automation routed by name prefix
- automation routed by direct `area_id`
- automation routed by `device_id`
- automation routed by referenced `entity_id`
- automation routed through called `script.*`
- cross-area automation that must become `_mixed`
- item with no route that must become `unknown`
- empty scenes file
- non-empty scene fixture for compose and integrity checks
