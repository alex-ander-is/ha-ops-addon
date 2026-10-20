# Dormant native folder discovery

Native activation remains deferred to step 24. `NativeScope` is never imported
or dispatched by the production server. The first-install folder UI is rendered
only when a qualified isolated adapter supplies `view.native_discovery`; query
parameters do not enable a backend. Installed legacy targets remain outside this
contour. The browser authorization entry is independent of folder setup.

`build(proposed, expected_revision=...)` persists **discovery rules only**. It
never selects files, captures publication blobs, stages/removes tracked files,
changes Git refs, publishes, or writes live configuration. No HA Save or native
baseline is required. A fresh installation defaults to no included folders; an
old established store inherits its published allowlist as the discovery default.
The original `active_scope`, baseline anchors and completed-worker proof remain
unchanged by discovery edits.

Rules are canonical sorted `(byte_folder_path, include_bool)` pairs. The longest
matching component prefix wins, with default excluded. For example, include A,
exclude A/B, and include A/B/C retain A's direct files and C while excluding B's
other descendants. A rule identical to its nearest ancestor is redundant and
refused by the backend. The reusable Vaadin component normalizes subtree toggles
and removes deeper overrides when a folder is checked or unchecked. IDs use the
canonical native byte codec; labels are escaped display text and never authority.
Known secret/private/recovery paths always remain excluded. Inventory traverses
excluded folders only as scaffolding for deeper includes and does not descend
into unrelated excluded subtrees. Databases, WAL, logs and caches are allowed;
the UI reports their size/warnings and explicitly unknown Git history/disk cost.
Calculate and Compact are future work, without placeholder action buttons.

Canonical discovery stores effective policy, previous confirmed policy, pending
review ID/local-root commitment and a monotonically increasing revision. Local
folder review, confirmation and cancellation never require a remote observer or
a first Git commit; empty/offline repositories remain usable for setup. A pending
policy immediately supplies fresh Save and Apply comparisons. Confirm finalizes
it locally; cancel restores the previous policy with a newer revision. After
reload/restart the UI disables confirmation until the user explicitly selects
Review current discovery policy. This admitted state-only review retains the
same policy/pending ID and issues fresh volatile consent; it never hydrates old
consent automatically. Neither
requires optional ignore/tracking publication. Active operations, recovery debt,
unsafe topology, incomplete metadata and actual capacity failures remain gates.
Old pending scope publications require explicit reconciliation/cancel; they are
not interpreted as checkbox consent.

Every policy write uses an exact `PreparedScope` state-only recipe, the existing
`scope_control` disk forecast/admission, full canonical atomic overlap, held
store lock, source review identity and state CAS. Missing-store admission binds
proved absence instead of initializing a baseline. Atomic fault/reopen retains
old or new valid policy without creating any content authority. No new physical
resource class or content payload recipe was introduced.

## Explicit whole-file operations

Fresh Preview always has empty file selection, parent approvals and choices.
Discovery membership never authorizes content effects. Selected Save and Apply
identities bind the original published baseline **and** the exact effective
rules/revision/exclusion digest. Old candidates refuse after edits, cancel,
change-back, branch/source movement, refresh, restart or update. Historical
publication codecs accept missing new optional fields, preserving old proof.

The dormant Preview `select(..., resolutions={byte_path: 'git'|'ha'})` accepts
choices only for explicitly selected files under discovery. No choice retains
the established three-way merge/conflict behavior. An explicit choice pairs
that side's complete bytes or reviewed absence with its numeric leaf metadata.
Save/git preserves the remote file; Apply/ha retains the live file without a
write. Required parent metadata still needs separate approval. Choices resolve
reviewed merge ambiguity only; secrets, unsafe types, unavailable metadata,
backup/disk/service/recovery/direct-access gates remain in force. A changed live
preimage or approved parent after a whole-file choice refuses execution instead
of silently overwriting newer state. Choice tuples are ephemeral immutable
review authority and cannot be replayed from a serialized artifact.

Refresh/restart/update retain discovery but clear file drafts, parent approvals,
directional choices, publication grants and local policy review grants. No
lifecycle event fetches, rebuilds, publishes or applies. Token storage is outside
discovery schemas and is never reset by folder actions.

## Retired scope publication and verification

The old scope builder implicitly selected newly included contents and removed
excluded remote files. Its public preparation/build/reopen entry points are
retired. Persisted v1/v2 records retain diagnostic, explicit cancellation and
already accepted in-flight publication recovery support. Test-only historical
record constructors preserve streaming, corruption, forgery, lifecycle, GC,
capacity and authenticated recovery coverage; they do not enable production
legacy grants.

Run real Linux scope and restore proof through the repository runner:

```sh
python3 dev/virtualization/ha_ops_dev.py run --input . -- \
  env GIT_AUTHOR_NAME=Fixture GIT_AUTHOR_EMAIL=fixture@example.invalid \
      GIT_COMMITTER_NAME=Fixture GIT_COMMITTER_EMAIL=fixture@example.invalid \
  python3 -m pytest -p no:cacheprovider -q \
    ha-ops/tests/test_native_scope.py ha-ops/tests/test_native_apply_integration.py
```

The selected restore regression includes a divergent or entirely absent folder,
no prior HA Save, nondefault numeric owner/full modes and required parent
metadata, ordinary authenticated readiness/backup/service/recovery participants,
an unselected sibling and an unchanged independent remote. Separate Save proof
publishes only its selected file and preserves original baseline provenance.
Host skips cannot qualify Linux allocation or ownership behavior.

The shipped-bundle browser flow uses the dedicated persistent Playwright profile
and a disposable Linux backend with actual canonical persistence. Its localhost
relay uses bounded task-owned file IPC; no network is enabled in the runner.
Only recorded owned tabs/processes are closed. Screenshots and DOM proof cover
nested mixed-state selection, click/Space, reload/restart, cancel, mobile bounds,
disabled styling, auth-entry independence and the native/legacy rendering gate.

```sh
/Users/purportex/Applications/Playwright/bin/playwright-node \
  ha-ops/tests/browser/folder-scope.mjs
```

Set `HA_OPS_BROWSER_CDP_URL` when reusing an already running dedicated browser and
`HA_OPS_BROWSER_ARTIFACTS_DIR` for evidence output. The fixture's opaque auth
sentinel is a relay isolation check; the real remembered-token persistence
regression and direct-access tests supply authorization-store proof.
