# Hunk Selection ToDo

Goal: let HA Ops users accept or keep individual changed hunks instead of
choosing a whole file, without letting the browser author patch text or final
file content.

## MVP scope

- Support hunk selection only for clean, managed text modifications that exist
  on both sides.
- Keep file-level decisions for conflicts, binary files, add/delete, rename,
  copy, mode changes, organizer heap outputs, and ambiguous normalized changes.
- For an eligible path, hunk mode replaces file-level controls:
  - Save offers `Take from HA` / `Keep Git`.
  - Apply offers `Take from Git` / `Keep HA`.
- Selecting a hunk selects its path; deselecting the last hunk deselects the
  path.
- Do not allow file-level and hunk-level decisions to coexist for the same path.

## Server authority and freshness

- On every Preview, build a server-owned raw hunk map with:
  - direction and target;
  - path and file mode;
  - before/after SHA-256;
  - exact raw hunk lines;
  - ordinal and stable operation ID;
  - complete raw map hash.
- Treat display normalization as presentation only. The UI may choose a
  server-issued operation ID, but must not send patch text, line ranges, or
  resulting file content.
- On Confirm, rebuild the Preview and require the normal commit/live
  fingerprints plus the raw hunk map hash to match.
- Reject unknown or stale operation IDs.
- If `git apply --check` fails, clear hunk state and make no Git or live HA
  change.

## Materialization

- Use a fresh private `GIT_INDEX_FILE` for every partial Save or Apply.
- Never touch the normal checkout index, working tree, or unrelated staged
  content while materializing selected hunks.
- Partial Save creates a single-parent commit from `repo_branch`; do not merge
  `ha-ops/ha-live`, because that would hide retained HA hunks.
- Partial Apply starts from the exact `ha-ops/ha-live` tree. After successful
  live write, export actual live HA and commit it as a single-parent child of
  the previous `ha-ops/ha-live`, so unselected Git hunks stay visible in later
  previews.

## Organizer and registry storage

- Use file-level fallback for organizer heap and derived metadata paths.
- Allowlisted `.storage` files may use hunks when the organizer is enabled.
- Registry storage may use semantic raw-record groups where safe:
  - derive operations from raw old/new records;
  - group a changed device with all changed active entities whose old or new
    `device_id` references that device;
  - before writing, validate final JSON and require every active entity
    `device_id` to reference an existing resulting device registry entry.

## Rollback and lifecycle

- For Apply, create the normal release snapshot only after final hunk validation
  and before the first live write.
- Record in `release.json`: pre/post live service commits, raw map hash,
  selected IDs, fallback decisions, affected paths, and snapshot status.
- Rollback restores the snapshot; do not try to apply inverse patches.
- Clear hunk state on Preview, Cancel, Confirm completion/failure, stale
  rebuild, Reset Git State, refresh, restart, version update, direction change,
  and switching back to file mode.

## Required regression tests

- Same-file partial Save and Apply preserve omitted modification, insertion,
  and deletion hunks for the next Preview.
- Private-index staging leaves the normal index and working tree untouched.
- Unknown/stale operation IDs and failed patch checks write neither Git nor live
  HA.
- Raw registry changes hidden by display normalization invalidate the map.
- Device/entity additions, removals, and moves close correctly; dangling
  references fail before live work.
- Organizer heaps, conflicts, binaries, add/delete, rename, copy, and mode
  changes use file-level fallback.
- Hunk state clears on every lifecycle transition listed above.
