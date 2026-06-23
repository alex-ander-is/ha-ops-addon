# Organizer Round-Trip Testing Todo

Status: planning artifact for future `.ha-ops/areas` organizer work. Keep this
document aligned with `organizer-contract.md` and
`service-branch-merge-contract.md`.

## Goal

Build a testing model that proves the organizer is a stable two-way projection
between Home Assistant heap files and HA Ops area files.

The tests must cover both directions:

- Live Home Assistant heap state saved to Git as `.ha-ops/areas`, then applied
  back to live Home Assistant, must show no live Home Assistant difference.
- Git `.ha-ops/areas` state applied to live Home Assistant, then saved back to
  Git, must show no Git difference.

## Scope Boundary

- [ ] Keep `.ha-ops/areas/**` limited to Home Assistant heap YAML:
  `automations.yaml`, `scripts.yaml`, and `scenes.yaml`.
- [ ] Do not split `.storage/core.area_registry`,
  `.storage/core.device_registry`, or `.storage/core.entity_registry` by area in
  this phase.
- [ ] Treat those registry files as routing inputs and ordinary `.storage`
  sync targets only.
- [ ] Keep existing registry diff normalization separate from organizer
  heap-to-area projection.
- [ ] Revisit registry area splitting only as a separate feature with its own
  graph-state contract and tests.

## Organizer State Model

- [ ] Treat area file paths as organizer metadata, not live Home Assistant
  state.
- [ ] Treat item identity plus item payload as the live heap state for
  automations, scripts, and scenes.
- [ ] A route-only move between area files must not change the live heap during
  Git-to-HA.
- [ ] A payload edit combined with a route change must apply the payload edit to
  the existing live heap position.
- [ ] Git-to-HA must not blindly compose heap files by sorted area directory
  order when a live heap baseline is available.

## Oracle Decision

- [ ] Decide whether the round-trip oracle is semantic or text/golden.
- [ ] If semantic, document which differences are ignored, especially
  automation and scene list order.
- [ ] If text/golden, define a canonical YAML format and stable item ordering.
- [ ] Define whether `organizer-index.json` is authoritative input, generated
  metadata, or both.
- [ ] Define how stale or mismatched `organizer-index.json` must behave.

## Current YAML Style Baseline

The current organizer implementation already has useful YAML style work that
future round-trip tests should preserve instead of replacing blindly.

- The organizer uses a custom `UniqueKeyLoader` that rejects duplicate mapping
  keys and keeps Home Assistant time-like values such as `21:00:00`,
  `10:00:00`, and `06:00:00` as strings.
- The custom loader keeps normal integer parsing for non-sexagesimal values
  such as `42`, `0x10`, and `0755`.
- The dumper prefers `annotatedyaml.dumper.dump` when available, matching Home
  Assistant's YAML style more closely than plain PyYAML.
- The fallback dumper uses block style, preserves key order, allows Unicode,
  and does not sort keys.
- Both dumper paths normalize Home Assistant null style by rewriting
  `: null` to an empty value.
- Existing tests already cover time-like strings, non-sexagesimal integers,
  null cleanup, single-line Jinja templates, multi-statement Jinja strings, and
  notification titles with escaped Unicode.

Required follow-up tests:

- [ ] Add a real snapshot YAML style fixture with representative Home Assistant
  automations, scripts, and scenes.
- [ ] Prove `heap -> areas -> heap` does not churn quoting, folded/block
  scalars, template strings, time-like strings, null values, Unicode escapes,
  list indentation, or mapping key order under the chosen oracle.
- [ ] Prove `areas -> heap -> areas` does not churn the same YAML style cases.
- [ ] Include both `annotatedyaml` and fallback dumper behavior where practical,
  or explicitly document which runtime is authoritative for add-on tests.
- [ ] Add regression coverage before changing loader or dumper behavior.
- [ ] Keep style tests separate from routing tests so failures identify whether
  the bug is projection/routing or YAML serialization.

## Synthetic Contract Fixtures

- [ ] Keep focused synthetic fixtures for routing rules and integrity failures.
- [ ] Cover UI area ownership over referenced devices/entities.
- [ ] Cover organizer overrides.
- [ ] Cover prefix routing.
- [ ] Cover direct `area_id` references inside an item.
- [ ] Cover referenced `device_id` resolved through `core.device_registry`.
- [ ] Cover referenced `entity_id` resolved through `core.entity_registry`.
- [ ] Cover referenced `script.*` service calls.
- [ ] Cover `.mixed` when multiple candidate areas tie.
- [ ] Cover `.unknown` when no route exists.
- [ ] Cover duplicate automation ids, script keys, and scene identities.
- [ ] Cover malformed area YAML before writing heap files.
- [ ] Cover nested unmanaged heap-like files under `.ha-ops/areas`.

## Real Snapshot Fixtures

- [ ] Add committed real snapshot fixtures derived from `ha-config`.
- [ ] Store a heap snapshot with `automations.yaml`, `scripts.yaml`,
  `scenes.yaml`, and the required `.storage` registries.
- [ ] Store an approved area snapshot with `.ha-ops/areas/**` and
  `organizer-index.json`.
- [ ] Document how to refresh snapshots from `ha-config`.
- [ ] Require manual review before replacing approved snapshots.
- [ ] Keep CI independent from the current live Home Assistant instance.

## Optional External `ha-config` Test Mode

Synthetic fixtures must always run. External `ha-config` tests are optional and
must run only when the user explicitly provides a fixture path.

- [ ] Add an opt-in environment variable such as
  `HA_OPS_HA_CONFIG_FIXTURE=/path/to/ha-config`.
- [ ] Skip external `ha-config` tests when the environment variable is absent.
- [ ] Validate that the provided path has a Home Assistant source root, normally
  `homeassistant/`.
- [ ] Treat the provided `ha-config` repository as read-only test input.
- [ ] Copy the required files into a temporary directory before any mutation,
  split, compose, Save, or Apply simulation.
- [ ] Never write generated fixtures, heap files, area files, or Git state back
  to the external `ha-config` path.
- [ ] Support external fixtures that currently use heap files only.
- [ ] Support external fixtures that currently use organized `.ha-ops/areas`
  files.
- [ ] Run the same low-level and full-flow round-trip assertions against the
  external fixture after copying it to temporary storage.
- [ ] Keep default CI and default developer test runs independent from private
  `ha-config` content.
- [ ] Treat failures in optional external mode as local regression signals; make
  them release-blocking only if the fixture is explicitly configured in that
  CI environment.
- [ ] Redact or avoid logging secrets from external fixtures, including
  `secrets.yaml`, auth files, tokens, and unrelated `.storage` content.

## Low-Level Round Trips

- [ ] Test `heap fixture -> split_live_heaps_to_git -> compose_git_view_to_live`
  against the chosen heap oracle.
- [ ] Test `area fixture -> compose_git_view_to_live -> split_live_heaps_to_git`
  against the chosen area oracle.
- [ ] Verify item counts in both directions.
- [ ] Verify automation ids, script keys, and scene identities in both
  directions.
- [ ] Verify payload preservation in both directions.
- [ ] Verify no item is duplicated across area files after either direction.
- [ ] Verify no item disappears into an unreferenced file.

## Full Save/Apply Flow Round Trips

- [ ] Test `live heap fixture -> Save HA to Git preview/save -> Apply Git to HA
  preview` returns no live diff.
- [ ] Test `git area fixture -> Apply Git to HA preview/apply -> Save HA to Git
  preview` returns no Git diff.
- [ ] Include service branch preview state in both tests.
- [ ] Include selected-path state in both tests.
- [ ] Include `organizer-index.json` changes in both tests.
- [ ] Include non-organizer Home Assistant files so the test proves organizer
  normalization does not hide adjacent real diffs.
- [ ] Verify registry files stay whole files and are not emitted under
  `.ha-ops/areas`.

## Required HA-to-Git Risk Cases

- [ ] Incomplete `core.entity_registry` sends an item to `.unknown`, then a
  registry update routes the same payload to a real area as a route-only diff.
- [ ] Incomplete `core.device_registry` sends an item to `.unknown`, then a
  registry update routes the same payload to a real area as a route-only diff.
- [ ] A Home Assistant UI area change moves an unchanged item between area
  files without changing its heap payload.
- [ ] A route-only move caused by changed routing hints shows only a move
  between area files and no payload diff.
- [ ] An item with references to multiple areas routes to `.mixed` when no
  owner exists.
- [ ] A small referenced entity/action change changes the fallback route while
  preserving the item identity.
- [ ] A payload edit that also changes routing hints shows both the payload
  edit and any area-file movement without duplicating the item.
- [ ] An automation referencing `script.*` uses the called script as a fallback
  routing hint only when no owner exists.
- [ ] Real Home Assistant area `Unknown` routes to `unknown/`, not `.unknown/`.
- [ ] YAML style stays stable for templates, block scalars, quoted values, and
  Home Assistant null formatting.
- [ ] YAML style stability uses the current Home Assistant-compatible dumper
  baseline instead of ad hoc string rewrites outside the organizer YAML helpers.
- [ ] `organizer-index.json` is regenerated from the saved area files and does
  not drift from the routed payload.
- [ ] Registry noise normalization does not hide a real routing change.

## Required Git-to-HA Risk Cases

- [ ] Existing automation identities replace the matching live heap item in its
  original heap position.
- [ ] Existing script keys replace the matching live heap mapping entry without
  moving surrounding entries.
- [ ] Existing scene identities replace the matching live heap item in its
  original heap position.
- [ ] New automations are appended after existing live heap items.
- [ ] New scripts are appended as new mapping keys after existing live entries.
- [ ] New scenes are appended after existing live heap items.
- [ ] Automation `id` changes are treated as delete plus add, not as an in-place
  edit.
- [ ] Script key changes are treated as delete plus add, while alias-only
  changes are in-place edits.
- [ ] Scene identity changes are treated as delete plus add, especially for
  scenes that rely on `name` because `id` is missing.
- [ ] Full apply deletes live items that are absent from the Git area view.
- [ ] Partial apply preserves live items outside the selected item scope even
  when they are absent from selected area files.
- [ ] Partial apply cannot apply an item file and a stale or unrelated
  `organizer-index.json` as independent user decisions.
- [ ] Duplicate identities across area files fail before any live heap write.
- [ ] Applying heap changes together with `.storage` changes does not let
  registry side effects create false Save diffs on the next HA-to-Git pass.
- [ ] Protected storage and non-heap Home Assistant files remain visible when
  they have real diffs adjacent to organizer changes.
- [ ] Route-only area movements remain invisible in Apply preview when the
  composed heap is unchanged.
- [ ] Route-only area movements do not reorder live heap items during Apply.
- [ ] Real payload edits remain visible in Apply preview even when routing also
  changes.
- [ ] Real payload edits combined with route changes update the existing live
  heap item in place rather than deleting and appending it.
- [ ] Multiple-reference fallback changes from one area to `.mixed`, `.unknown`,
  or another area remain live-no-op when identity and payload are unchanged.

## Mutation Suite

- [ ] Add automation.
- [ ] Delete automation.
- [ ] Rename automation alias while preserving `id`.
- [ ] Change automation payload.
- [ ] Add script.
- [ ] Delete script.
- [ ] Rename script alias while preserving YAML key.
- [ ] Change script payload.
- [ ] Add scene.
- [ ] Delete scene.
- [ ] Change scene payload.
- [ ] Change UI area in `core.entity_registry`.
- [ ] Remove UI area so fallback routing is used.
- [ ] Add cross-area references.
- [ ] Manually move an item between area files.
- [ ] Introduce stale `organizer-index.json`.
- [ ] Remove `organizer-index.json`.
- [ ] Duplicate an item across area files.
- [ ] Add invalid nested `.ha-ops/areas/<area>/nested/automations.yaml`.

## Implementation Cleanup Targets

- [ ] Prefer one canonical organizer projection comparison over scattered
  route-only diff suppression.
- [ ] Make unknown-base Save conflict detection compare logical organizer items
  where possible, not only raw paths.
- [ ] Decide whether list order is state and update fingerprints/tests
  accordingly.
- [ ] Decide whether `organizer-index.json` must be regenerated or validated
  during compose.
- [ ] Keep unmanaged area documents, such as local contracts, out of apply/save
  diffs.
- [ ] Keep the organizer opt-in behavior explicit in tests.

## Acceptance Gate

- [ ] Focused synthetic organizer tests pass.
- [ ] Real snapshot low-level round-trip tests pass.
- [ ] Real snapshot full Save/Apply round-trip tests pass.
- [ ] Mutation tests pass for both directions.
- [ ] No test or implementation emits split registry files under
  `.ha-ops/areas`.
- [ ] A reviewer can point to the oracle decision when judging a diff as real
  or ignored.
