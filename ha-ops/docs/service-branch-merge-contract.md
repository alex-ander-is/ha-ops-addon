# HA Ops Service Branch Merge Contract

Status: agent-facing contract for HA Ops 0.8.x. Keep implementation, tests, and
reviews aligned with this document.

## Why This Exists

HA Ops 0.8.x stopped treating Save and Apply as direct two-directory copies.
Both directions now use Git branches to model the user-managed Git state and the
latest exported live Home Assistant state.

This is intentionally more complex than the old flow. Several operations that
look surprising during review are part of the design, and several adjacent
shortcuts are unsafe.

## Branch Model

- `repo_branch`, usually `main`, is the user-managed branch.
- `ha-ops/ha-live` is the latest exported live Home Assistant and selected
  App config.
- `ha-ops/base` is a marker branch pointing at the current merge base between
  `repo_branch` and `ha-ops/ha-live`.

Preview jobs may create commits on `ha-ops/ha-live` and push HA Ops service
branches. That is expected. It changes Git service refs only; it must not write
live Home Assistant config.

Reset Git State is a recovery action for stale or inconsistent preview graphs.
It must not rewrite `repo_branch` and must not write live Home Assistant config.
It rebuilds `ha-ops/ha-live` from the current `repo_branch` plus a fresh live
export, moves `ha-ops/base` to the resulting merge base, and force-with-lease
pushes only those HA Ops service branches.

Apply and Save jobs may leave the local checkout on a service branch after a
service-branch commit. That is acceptable because the next normal job calls
`ensure_repo(...)`, which checks out `repo_branch` again before user-branch work.

When a local service branch and `origin/<service-branch>` both exist, the
origin ref is preferred while rebuilding previews. This avoids re-pushing stale
local service branch history after another HA Ops instance or previous preview
updated the remote service ref.

## Save HA to Git

Save direction merges live HA into Git:

```text
checkout repo_branch
merge --no-commit ha-ops/ha-live
```

When Save accepts every previewed path from HA, the resulting user-branch commit
may be a merge commit with `ha-ops/ha-live` as a parent. When Save keeps any
previewed path unchanged from Git, the resulting user-branch commit must be a
single-parent commit based on `repo_branch` only. Otherwise Git marks the full
live export as merged and later Save previews hide the unselected HA changes.

Conflict stage meanings for Save:

- stage 2 is Git, from `repo_branch`
- stage 3 is HA, from `ha-ops/ha-live`

Choosing HA for a Save conflict means stage 3. Choosing Git means stage 2 or a
delete when the Git side deleted the path.

Save must rebuild the preview before writing to `repo_branch`. If the stored
preview commit or fingerprint no longer matches and the rebuilt preview still
has changes, Save must write an updated warning preview to state and stop
without committing to `repo_branch`. If the rebuilt preview is empty, Save must
finish as a no-op and clear stale preview selections.

After a successful Save, the stored Save preview must be rebuilt and written to
state. Files committed by the Save must disappear from the Change List
immediately, while unselected live changes remain visible for a later Save.

`save_push_retry_pending` is intentional. If Save already created the
user-branch commit but push failed, the next Save should retry the push instead
of exporting live HA again and creating a second commit.

Save push recovery must preserve merge commits. Do not use a plain rebase on a
local branch that can contain an unpushed Save merge commit, because it can
flatten the HA live parent out of history.

## Apply Git to HA

Apply direction merges Git into live HA:

```text
checkout ha-ops/ha-live
merge --no-commit repo_branch
```

Conflict stage meanings for Apply:

- stage 2 is HA, from `ha-ops/ha-live`
- stage 3 is Git, from `repo_branch`

Choosing HA for an Apply conflict means stage 2. Choosing Git means stage 3 or a
delete when the Git side deleted the path.

Conflict previews are applied from the resolved `ha-ops/ha-live` worktree. That
is why `commit_apply_merge(...)` runs before `apply_targets(...)` for conflict
previews.

Non-conflict apply previews use `selected_apply_targets_from_preview(...)` to
materialize any per-path HA choices into a temporary source tree. Their
`ha-ops/ha-live` commit is created after live Apply succeeds.

Overlay apply cannot delete a live file. For conflict previews where the user
chooses a Git-side delete, `delete_apply_conflict_live_deletions(...)` performs
the selected live deletion after the normal overlay apply.

## Preview State and Staleness

Preview state is a safety gate, not UI cache.

Save uses:

- `last_save_preview_commit`
- `last_save_preview_fingerprint`
- `last_save_preview_paths`
- `last_save_preview_conflicts`
- `save_preview_resolutions`

Apply uses:

- `last_preview_commit`
- `last_preview_fingerprint`
- `last_preview_live_fingerprints`
- `last_preview_paths`
- `last_preview_conflicts`
- `apply_preview_resolutions`

Both directions also track the preview paths selected for processing:

- `save_preview_selected_paths`
- `apply_preview_selected_paths`

Fresh previews must initialize these selected-path lists to empty. Missing
selected-path state must also behave as empty, not as select-all. This makes the
UI default safe for large change sets: a file shown in the Change List is
visible and inspectable, but it is not processed by Save or Apply until the user
checks it.

For conflict previews, fingerprints must include both sides of each conflicted
path. A path-only fingerprint is unsafe because live HA or Git content can
change while the path list stays the same.

For non-conflict Apply previews, `last_preview_live_fingerprints` protects live
Home Assistant heap files from changing after Preview and before Apply.

## Conflict Preview Rules

Conflict previews must put every changed file in the Change List, including
cleanly merged files. They separately track the subset of conflicted paths that
requires an explicit HA/Git choice.

Conflict previews require explicit HA/Git choices for every selected conflicted
path. The UI disables Confirm until all selected conflict paths are resolved,
and the server must reject direct Save or Apply submissions when selected
conflict choices are missing.

Unselected preview paths must keep the current side and must not be processed:

- Save keeps Git for unselected paths.
- Apply keeps live HA for unselected paths.

Selected non-conflict preview paths use these defaults unless the user chooses a
different side:

- Save defaults unresolved paths to HA.
- Apply defaults unresolved paths to Git.
- Resolving a non-conflict path updates the selected side only. Save or Apply
  starts only from the explicit Confirm action.

Conflict previews must not hide cleanly merged changes. If a merge has one
conflicted path and one cleanly merged path, the cleanly merged path still must
be visible in the preview and covered by the fingerprint. Otherwise Save or
Apply can change a file the user never saw. Do not document that behavior as
intentional; fix it and add regression tests.

Regression tests for this invariant should cover both directions:

- Save conflict plus clean HA-only addition is shown before it is committed.
- Apply conflict plus clean Git-only addition is shown before it is written to
  live HA.

## Protected Storage Approval

Allowlisted `.storage` files are shown in previews. Protected files such as
`core.device_registry`, `core.entity_registry`, and `person` may be saved to Git
and may be applied to HA only after the matching preview decision flow.

In 0.8.x the preview decision and Confirm flow is the explicit approval for
previewed `.storage` paths. There is no separate `/approve-apply` approval path.

When conflict preview storage paths are confirmed, Apply must use
`approve_storage_apply_targets(...)` so the resolved protected storage content
is actually written instead of being skipped after the UI reported success.

## Registry Noise Normalization

Registry normalization is intentional diff noise suppression. It must hide only
known volatile or order-only registry noise and must not hide real registry
state changes.

Useful reference: `ha-ops/docs/diff-stability-plan.md`.

`include_redundant_data` intentionally bypasses the normal reduced diff view so
Save can preserve registry data exactly as Home Assistant writes it.

## Organizer Interaction

Do not treat missing root heap files as absent automations, scripts, or scenes
when organizer is enabled. The organized area view may be authoritative in Git:

```text
homeassistant/.ha-ops/areas/*/automations.yaml
homeassistant/.ha-ops/areas/*/scripts.yaml
homeassistant/.ha-ops/areas/*/scenes.yaml
homeassistant/.ha-ops/areas/organizer-index.json
```

Useful reference: `ha-ops/docs/organizer-contract.md`.

## UI State That Is Intentional

Disabled button styling is global and intentional. New disabled buttons should
inherit the pale gray background, muted text, muted border, and full opacity
rule from `ui.py`.

The conflict preview Confirm button is disabled only for conflict previews with
missing choices. Non-conflict preview rows are a convenience selector, not a
mandatory all-path approval list.

## Review Checklist

Before approving changes to the 0.8 service-branch flow, check these points:

- Preview jobs do not write live Home Assistant config.
- Save rebuilds preview and refuses stale preview state before committing.
- Apply rebuilds preview and refuses stale preview state before writing live HA.
- Conflict fingerprints include stage 2 and stage 3 content.
- Conflict previews show all files that Save or Apply can change, including
  cleanly merged paths.
- Modify/delete conflicts correctly remove paths when the selected side deleted
  them.
- Protected `.storage` paths that the UI says are approved are actually applied.
- Failed Save before commit cleans the checkout; failed Save after commit can
  retry push without creating another commit.
- Service branch pushes are either successful or reported clearly; do not hide a
  failed user-branch push.
- Tests cover the exact branch/stage direction being changed.
