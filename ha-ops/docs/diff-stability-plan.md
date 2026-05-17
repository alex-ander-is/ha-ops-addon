# Diff Stability Plan

## Goal

Reduce noisy HA Ops diffs without hiding real Home Assistant configuration changes.

## Scope

Start with Home Assistant device registry saves and previews:

- `homeassistant/.storage/core.device_registry`
- `data.devices[].connections`

Do not normalize arbitrary lists.

## First Step

Treat `data.devices[].connections` as an unordered set for diff and save comparison.

Stable normalization:

1. Parse registry JSON.
2. For each device object, sort only `connections`.
3. Sort each `connections` item by compact JSON representation.
4. Compare normalized data for preview.
5. Save normalized Git-view output only when the registry file is already being exported.

## Explicit Non-Goals

- Do not change live `/config/.storage/core.device_registry` just to reorder arrays.
- Do not sort all arrays globally.
- Do not normalize `identifiers` in the first implementation.
- Do not hide changes in device names, areas, config entries, labels, deleted devices, or metadata.

## Tests

Add coverage for:

- same `connections` values in different order produce no preview diff;
- save does not create a commit for `connections` order-only changes;
- real added or removed `connections` still produce a diff;
- unrelated arrays keep their original order.

## Later Candidates

Evaluate `data.devices[].identifiers` separately after observing real HA output. It appears set-like, but it should not be folded into the first change.
