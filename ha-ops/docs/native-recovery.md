# Dormant native recovery points (6.15)

`app/native_recovery.py` is an explicit-call primitive for isolated fixtures.
Nothing imports it from production. It does not run Apply, stop/start services,
change the UI, install updates or restore anything on startup.

A caller supplies an already qualified `native_metadata.Binding`, the initialized
native data anchors, and a nonempty dictionary of byte paths to `File`,
`Directory` or `None`. `File` contains exact caller-supplied bytes and numeric
UID, GID and full `07777` mode. `None` means deletion. Required parents and their
absence are included automatically. Explicit directory metadata changes must be
included in that dictionary. Directory-to-file changes must explicitly select
every removed child; arbitrary recursive deletion is never used.

```python
store = RecoveryStore(binding)
prepared = store.prepare({b'homeassistant/example.yaml': File(
    b'enabled: true\n', uid, gid, 0o640)})
point = store.install(prepared)
store.execute(operation_id=point['manifest']['operation'],
              owner_id=point['manifest']['owner'])
# A later process discovers only. Recovery always requires an explicit call.
reopened = RecoveryStore(binding)
pending = reopened.discover()
reopened.rollback()
reopened.rollback()  # Completed rollback is read-only and does not consume the point.
```

`prepare()` reads and compiles; a copied or reconstructed `Prepared` grants no
write authority. Immediately before any new generation allocation, `install()`
reopens and validates the active point under the same stable lock. An unchanged
pointer alone cannot retire a point: its signed journal must still be ready,
applied or restored with no pending intent. Unrelated edits to an old terminal
point do not block a disjoint replacement; new selected preimages and routes
still must match. An interrupted forward or rollback retains the old point.
`install()` independently streams and verifies recovery bytes,
then stages identifiable forward and rollback inodes on each actual target
volume. Staging pools are private directories below the nearest existing stable
ancestor, outside directories selected for removal. The shared inventory policy
reserves every `.ha-ops-recovery` or `.ha-ops-recovery-*` path component,
including stale or malformed pool names and descendants. These private bytes
never enter generated Git allowlists or pass complete-index audits; they are
also refused as recovery targets. Internal descriptor reads remain qualified
by the explicit recovery binding. A file is chowned, finally
chmodded and fsynced before publication. A new directory remains traversable
until descendants finish; final directory metadata is applied deepest first.
Symlinks, special files, multiply linked regular files, unconfirmed mounts,
repository-only files and unsupported allocation profiles refuse.

Each generation has immutable content blobs and a manifest. The active pointer
binds its manifest hash and exact generation inode. Installation never edits or
truncates the preceding point. All copies, staging, manifest and journal are
verified and durable before pointer replacement; the pointer's directory is
fsynced separately. A failure close to pointer publication leaves either the
old valid point or the complete new point. Failed writes and rollback retain
the active point.

The bounded journal records current owned states, actual changed paths and at
most one pending syscall. Intent is persisted and fsynced before a namespace,
chown or chmod effect. Rename postimages identify already staged inodes;
chown's possible special-bit clearing is explicit. Target removal uses an
addressed rename into an owned pool, preserving the
removed inode as a tombstone. Absence alone never proves our removal: the
original inode must exist at that tombstone. Rename, removal, chown and chmod
have independent intent/effect/readback/acknowledgment boundaries.
An error before acknowledgment does not imply that the syscall did nothing.
Reconciliation first fsyncs the exact target inode and every affected source
and destination directory; a failed durability retry leaves intent pending.
Explicit `reconcile()` compares the pending path with the exact before state or
qualified owned after state; any third state conflicts. Rollback uses only paths
with an actual effect receipt, in structural order. Untouched external edits
are preserved. Written paths, routes, supported metadata and staged inodes must
still match. A foreign inode with identical bytes is a conflict. A new child
prevents removal of its parent. Empty-directory membership is checked before
removal and after rename. If a child appears during the rename window, explicit
recovery restores the exact original directory inode before reporting conflict;
it never recursively deletes the child. Deleting an absent leaf preserves both
absent parents and existing unselected file ancestors. This is an observed-state
concurrency boundary;
it cannot detect an identical-state history by a cooperative same-UID writer.

The store holds a stable nonblocking flock throughout each mutating call.
Optional operation, owner and revision arguments fence stale callers. Strict
JSON decoding rejects duplicate/unknown keys, invalid types and inconsistent
phases, current states, stages or pending effects. Every discover verifies
preimage lengths and hashes by streaming; it performs no writes. Source capture,
copy and verification use bounded chunks rather than accumulating file content.
Source reads stop at the frozen expected size; an extra one-byte EOF probe is
never written. Every emitted chunk is checked against the remaining qualified
size before its first destination write, including caller-supplied byte chunks.
Source growth therefore conflicts without allocating unplanned output bytes.
Memory for caller-provided `File.data` belongs to the caller's exact target input.

`native_disk.recovery_project()` is a separate issuer-qualified recipe, sharing
the existing ext4/tmpfs conservative allocation profile. It takes one initial
snapshot and derives copies, per-volume stages, exact maximal control schemas,
atomic overlap, metadata, private parents and reserve. The initial attributed
baseline includes the old point until cleanup. The original forecast and floor
are retained in the immutable manifest. Private per-store authority seals bind
that forecast, the final manifest and every journal revision. `recovery_resume()`
verifies them and the original recipe, root,
layout, ownership and routes. It never reconstructs a generic `Plan` or weakens
ordinary `Admission`. Execute/reconcile/rollback do not call snapshot, projection,
whole-tree scans, Git enumeration or capacity queries.

Only after the new pointer is durable does installation reclaim inactive owned
generations. Stable signed preparation markers pin their own inode and record
generation,
pool, blob, manifest, journal, stage and tombstone identities. Journal signatures
also bind their published inode. Identical-content replacement inodes are never
cleanup authority. A torn own marker or an unacknowledged own control receipt can
be repaired only through immutable signed original-inode evidence. Cleanup
checks all known resources before
nonrecursive removal; a foreign entry, substituted inode or unacknowledged
allocation is retained as addressed debt. `cleanup()` explicitly retries that
debt under the same lock and never removes the active point. Partial cleanup is
idempotent. A killed private atomic write can retain its named scratch entry;
uncertain entries are preserved rather than guessed to be owned. Recovery bytes
are not consumed by a successful rollback. Refresh, process restart and version
update do not start recovery or clear the retained point.

## Verification

Run the focused root Linux group through the existing cached local runner:

```sh
python3 dev/virtualization/ha_ops_dev.py run --input . -- \
  python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_recovery.py
```

It covers exact content/absence/parents, both structural transitions, numeric
ownership/special modes, separate-process exits at each target syscall boundary,
strict corruption and foreign-edit refusal, point replacement and cleanup.
Host skips are not Linux qualification. The existing ext4 matrix additionally
requires a nested persistent-recovery physical peak proof, including transient
atomic controls and retained A/new B generations, plus separate child-process
exits before acknowledgment in both directions on ext4. A root Linux proof also
uses distinct real `/dev` and `/dev/shm` filesystems without adding mounts.
The byte profile requires real kernel ENOSPC during recovery-copy preparation, target-volume staging, pre-effect
intent persistence and post-rename acknowledgment. After freeing only the owned
filler, explicit reopen/rollback twice must restore exact bytes. All original
allocation/byte/inode rows and container recreation remain mandatory.

These are syscall/interruption and local ext4 qualification tests, not a
hardware power-loss simulation. There is no ACL/xattr, service lifecycle, live
Apply or UI integration in this step; the metadata contract remains the numeric
MVP supported by `native_metadata`.
