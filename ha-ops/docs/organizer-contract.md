# Home Assistant Organizer Contract

Status: implemented contract. The implementation must keep matching this
document.

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
  .unknown/
    automations.yaml
    scripts.yaml
    scenes.yaml
  .mixed/
    automations.yaml
    scripts.yaml
    scenes.yaml
```

The `.ha-ops/areas` tree is HA Ops managed metadata. It is not a Home Assistant
include tree and must not be applied to live Home Assistant as ordinary config.

## Activation

Organizer behavior is part of the Home Assistant target contract. The default
activation mode must be explicit in code and documentation:

- Organizer is opt-in. Save must leave heap files in Git until the target enables
  organizer explicitly.
- Changing the default activation mode is a migration change and must be called
  out in the changelog.

Supported target forms:

```json
{"organizer": true}
{"organizer": {"enabled": true}}
{"organizer": {"enabled": true, "organized_root": ".ha-ops/areas"}}
```

Missing `organizer`, `organizer: false`, and `organizer: {"enabled": false}` are
disabled for that target.

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

## Precedence

If Git contains both live heap files and the organized `.ha-ops/areas` view for
the same target, the organized view is authoritative for Apply. The heap files
are compatibility input only and must not override the organized view.

If Git contains no organized view, Apply uses the heap files directly.

Save may remove heap files from Git after writing the organized view, unless the
organizer options explicitly request keeping heap files.

## Conflict Model

Organizer conflicts are logical item conflicts, not raw `.ha-ops/areas` path
conflicts. The identity rules below define the conflict keys:

- automation `id`
- script YAML mapping key
- scene `id`, or `name` when `id` is absent

Unknown-base Save conflict detection must not treat a missing virtual
`.ha-ops/areas/<area>/*.yaml` file as a missing live Home Assistant file. When
possible, it should compare the composed heap representation and report
conflicts by item identity.

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
7. `.mixed` if multiple areas are equally plausible and no deterministic owner
   exists.
8. `.unknown` if no owner can be found.

Service buckets are dot-prefixed and reserved. Real Home Assistant areas must
not use dot-prefixed organizer directory names. This avoids collisions with
valid area names such as `Unknown`, which route to `unknown/`.

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
- `.mixed` and `.unknown` are explicit buckets, not silent guesses
- malformed split files fail before writing live Home Assistant heap files

Routing is advisory. Integrity checks are authoritative. A bad routing guess may
move an item to the wrong area file, but it must never cause item loss,
duplication, or direct application of the organized view as Home Assistant
config.

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
- cross-area automation that must become `.mixed`
- item with no route that must become `.unknown`
- empty scenes file
- non-empty scene fixture for compose and integrity checks
