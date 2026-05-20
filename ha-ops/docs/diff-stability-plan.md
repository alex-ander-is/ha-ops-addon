# Diff Stability Plan

## Goal

Reduce noisy HA Ops diffs without hiding real Home Assistant configuration changes.

## Evidence

Commit `e9cc6f0a17a0ed0c6713b225c49fd6b2bef07dae` is a useful sample because
Save produced only two Git file changes:

- `homeassistant/.storage/core.device_registry`: 21 insertions, 69 deletions.
- `homeassistant/.storage/core.entity_registry`: 582 insertions, 582 deletions.

Semantic breakdown:

- `core.device_registry`
  - `data.devices`: 183 before and after; same ids; same top-level order.
  - `data.devices[].connections`: 5 devices changed only by connection order.
  - `data.devices[].config_entries_subentries.*`: 1 device changed only by subentry order.
  - `data.deleted_devices`: 47 deleted device records were removed; this is real state.
  - `sw_version`: 3 real version changes.
  - `modified_at`: timestamp-only churn on 14 devices.
- `core.entity_registry`
  - `data.entities`: 1981 before and after; same ids; same top-level order.
  - `data.deleted_entities`: 1205 before and after; same order and content.
  - 582 entities changed `modified_at`.
  - 149 MQTT entities changed `suggested_object_id` only by adding a numeric suffix while `entity_id` stayed stable.
  - 8 mobile app entities changed `original_icon` with dynamic battery/wifi state.
  - 2 entities had meaningful changes after volatile fields were ignored:
    `media_player.bathroom_radio` capabilities and one MQTT sensor `disabled_by`/`options`.

This means registry diff noise has at least three separate classes:

1. Order-only arrays that should compare as sets.
2. Volatile metadata fields that can churn without changing the current entity
   or device contract.
3. Real registry state changes that must remain visible.

## Implemented Scope

Home Assistant registry saves and previews:

- `homeassistant/.storage/core.device_registry`
- `data.devices[].connections`
- `data.devices[].config_entries_subentries.*`
- `data.devices[].modified_at`
- `homeassistant/.storage/core.entity_registry`
- `data.entities[].modified_at`
- `data.entities[].suggested_object_id`
- `data.entities[].original_icon` for `platform == "mobile_app"`

Do not normalize arbitrary lists.

## Normalization Rules

Treat these arrays as unordered sets for diff and save comparison:

- `data.devices[].connections`
- `data.devices[].config_entries_subentries.*`

Treat these fields as volatile for diff and save comparison:

- `data.devices[].modified_at`
- `data.deleted_devices[].modified_at`
- `data.entities[].modified_at`
- `data.deleted_entities[].modified_at`
- `data.entities[].suggested_object_id`
- `data.entities[].original_icon` only for `platform == "mobile_app"`

## Explicit Non-Goals

- Do not change live `/config/.storage/core.device_registry` just to reorder arrays.
- Do not sort all arrays globally.
- Do not normalize `identifiers` in the first implementation.
- Do not hide changes in device names, areas, config entries, labels, deleted devices, or metadata.
- Do not hide `deleted_devices` or `deleted_entities` additions/removals.
- Do not hide `sw_version`, `disabled_by`, `options`, `capabilities`, `entity_id`, `unique_id`, `device_id`, `area_id`, or `name`.

## Tests

Add coverage for:

- same `connections` values in different order produce no preview diff;
- save does not create a commit for `connections` order-only changes;
- real added or removed `connections` still produce a diff;
- unrelated arrays keep their original order.
- volatile-only registry changes produce no preview diff;
- real deleted registry item changes still produce a diff;
- `sw_version`, `disabled_by`, `options`, and `capabilities` changes still produce a diff.

## Later Candidates

Evaluate `data.devices[].identifiers` separately after observing real HA output. It appears set-like, but it should not be folded into the first change.

Evaluate these separately:

- `data.devices[].identifiers` as unordered sets.
- `data.devices[].config_entries` as unordered sets if devices with multiple
  config entries appear.
- `data.entities[].capabilities.supported_color_modes` as unordered sets if it
  proves noisy in real saves.

Do not implement these as one broad normalization rule. Each candidate needs a
focused test showing that a real adjacent change still appears in Save Preview.
