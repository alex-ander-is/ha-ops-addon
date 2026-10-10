# Dormant native metadata adapter

`app/native_metadata.py` implements step 6.8's fixed Etckeeper attribute model
as strictly validated JSON data. It has no production import, initialization,
Apply executor, journal, UI state, migration or live-system activation. Existing
legacy behavior and native resource/accounting owners are unchanged. Importing
it does not execute a command, open a file, restore metadata or replay a draft.
There is consequently no new state to revive on refresh, restart or update.

## Fixed repository data

The exact tracked anchor is
`data/native/repository-files/etckeeper-metadata.json`. Its sole top-level field
is `entries`. Each row has exactly `path`, `kind`, `uid`, `gid`, `mode`, `oid`.
Paths are canonical standard-base64 Unix bytes, sorted by raw byte order.
`kind` is `file` or `directory`; numeric UID/GID are explicit integers including
zero, and mode is an integer containing all `07777` bits. Files bind a lowercase
full Git blob OID of the caller's repository width (40 or 64 characters);
directories have `oid: null`. There is no version/evolution field, shell text,
ACL/xattr, name lookup or implicit owner/mode default.

`decode` rejects malformed UTF-8/JSON, duplicate keys/paths, unknown or missing
fields, nonfinite values, booleans as integers, noncanonical base64, denied byte
paths, identities outside Linux's `0..4294967294` range, unsupported kinds and
incomplete/colliding directory closure. The `4294967295` unchanged sentinel is
excluded. `validate` additionally compares the entire managed file set, blob
identity and owner-executable bit against exact regular stage-zero Git entries.
The complete required directories are exactly the proper ancestors of those
files. Root, `.gitignore`, the manifest, service scaffolding and other designated
repository-only artifacts receive no restorable metadata row. Empty managed
trees have an explicitly empty manifest. Required prefixes of an explicit
additional live root are admitted by semantic closure without relaxing ordinary
leaf policy or secret/service fences.

`encode` validates with caller-supplied Policy/OID width. `Manifest.digest` and
`encoded_size` expose canonical supported-metadata SHA-256 and exact JSON byte
length for later fingerprint/accounting producers; use them on already validated
manifests. Neither changes NativeOperationStore or NativeDisk.

## Capture, review and isolated staging

The caller supplies a `Binding(root, policy, mounts, confirmed_mounts,
topology_verified)` and selected exact Git entries. `capture` reads only those
regular files and proper parents. No-follow descriptors and streamed Git-object
hashing bind inode metadata to actual matching bytes. Before/after stat, route,
link count and namespace checks reject observable replacement, retyping,
content or metadata changes. Capture writes no files or staging.

`select(baseline, fresh, selected, approved_parents=...)` overlays only selected
leaf rows/deletions. Every unselected leaf/OID and existing unapproved parent
stays exactly baseline. New required parents need explicit review; the adapter
never guesses current defaults. Approved parent rows produce `ParentReview`
values listing every dependent managed leaf, including unselected descendants.
Removing the last leaf prunes orphan manifest rows only, without removing a
filesystem directory. Replacing a file with descendants selects the former file
as a deletion, every added leaf, and explicitly reviews its new required parent.
The reverse replacement selects the new file and every removed old descendant;
unselected descendants cannot disappear or be silently overwritten by a parent
row. Approved parents must remain required and lie on a selected leaf route.
Both inputs must be complete format-valid manifests; staging checks their actual
Git correspondence.

`merge(base, ours, theirs)` merges whole per-path records and absence using
three-way equality. It never mixes owner/group/mode facets automatically.
Conflicts retain complete base/ours/theirs, differing facets and dependent
children. Structural closure/type conflicts also become path rows. A `Merge`
with unresolved rows cannot yield a complete validated manifest through
`resolved(tree)`. Explicit resolution requires a newly complete manifest/tree;
no decisions are persisted or replayed.

The caller creates NativeGit with the exact manifest Policy exception:

```python
policy = Policy(repository_files=(MANIFEST_PATH,))
git = NativeGit(root, policy=policy)
with git.isolated_index(base_tree, operation_id=operation, owner_id=owner) as index:
    tree = stage_candidate(git, index, selection, entries=selected_index_entries,
                           removals=selected_deleted_paths, width=repository_oid_width)
```

`stage_candidate` requires the existing baseline tracked manifest and validates
it against the complete index and selection baseline before any update. Baseline
comparison uses validated per-path records, so different enumeration order is
semantically identical while genuinely changed supported data remains stale.
Staging also reconstructs the entire candidate from its declared selected leaves
and explicit parent reviews: every unselected leaf remains baseline, unreviewed
parents remain baseline, and new parents need reviews. Parent review paths must
be unique and eligible; before/after records and the complete dependent-child
set must match the candidate. Foreign, duplicate, incomplete or inconsistent
review data is refused before blob/index writes. Public `Selection` data alone
is never treated as an approved boundary. The caller has already written/verified
selected blobs using NativeGit's streamed
blob seam. The helper stages selected content and reconstructed manifest in that
same private index. After complete input validation, selected removals precede
selected additions and the manifest update so NativeGit's single-call
ancestor/descendant fence also supports reviewed Git shape replacements. The
helper reads back the manifest blob, validates the complete result, and calls
`write_tree`. Directory rows never enter the index. The fixed exact
exception does not permit adjacent service paths. Failure produces no usable
candidate result: discard the private handle through its owning context. This
method creates neither a commit/ref nor canonical index changes; it does not
redesign NativeGit ownership or mutate journal-owned materialization trees.
Initial adoption must explicitly seed a complete baseline manifest in a separate
reviewed candidate; missing data is never silently inferred by this helper.
For an explicitly empty managed baseline, the caller can use the existing Git
seam before invoking selection/staging:

```python
empty = Manifest(())
with git.isolated_index(None, operation_id=operation, owner_id=owner) as index:
    blob = git.write_blob_stream(encode(empty, policy=policy, width=repository_oid_width))
    git.update_index(index, (IndexEntry(MANIFEST_PATH, b"100644", blob.oid, 0),))
    validate(empty, git.index_entries(index), policy=policy, width=repository_oid_width)
    baseline_tree = git.write_tree(index, allowed_paths=(MANIFEST_PATH,))
```

This is an explicit tracked empty JSON baseline, not adoption of current live
metadata. Selecting new files afterwards requires approved records for every
new proper parent. A nonempty baseline instead needs its complete captured and
reviewed manifest matching exact content before it can be used. Publication and
production bootstrap remain caller-owned future work.

## Metadata-only restoration

The caller first materializes exact historical regular-file bytes and proper
parents in an independently owned tree using Git objects. The adapter does not
create, replace, truncate or delete content/directories. Historical absent
objects must be materialized before `observe`/`restore`; missing/retyped objects
block before metadata writes.

Call `observe(binding, target_manifest, selected_files,
approved_parents=reviewed_parents, width=...)` to obtain fresh immutable `Before`
evidence covering every selected inode, approved parent and routing/root
ancestor. Then call `restore` with that evidence, the exact validated target
manifest/tree and the same explicit sets and binding. `restore` never invents
its own approval from arbitrary current occupants. Incomplete/stale evidence or
unrelated parent approvals block. UID/GID are numerical identities; there is no
NSS translation or semantic cross-host account portability claim. Valid IDs
absent from NSS are supported when destination privilege permits them.

All selected objects prevalidate before the first effect. Descriptors use
`O_NOFOLLOW`, directory flags and `O_NONBLOCK`, including protection against
raced FIFOs. Multiply linked regular files are refused to protect unselected
aliases. Each route compares explicit confirmed mount identity and actual
descriptor device; Linux additionally compares the current kernel mount record
with supplied facts, including same-device boundaries. Unconfirmed, unknown,
stale or replaced paths block. Descriptor count scales with path depth, never
selection size; content hashing is streamed.

Metadata order is files first, then approved directories deepest-first.
Immediately before ownership/mode effects the namespace is reacquired and
checked against held/reviewed identities. Descriptor `fchown` is used only when
ownership differs, followed by final `fchmod` even if the original mode already
matched. This restores special bits cleared by chown. Independent attribute and
content readback, fsync, namespace readback and successful descriptor closes are
required before a path counts as completed. Restrictive final parent modes are
applied after children. Unrelated siblings/parents are not changed.

A terminal `RestoreFailure` exposes its primary `cause` and `Effects` containing
verified `completed`, `attempted`, `uncertain`, `failed`, `unattempted` and
secondary cleanup diagnostics. Attempts are recorded before a potentially
effectful syscall. A successful syscall without independent verification or
required durability/close proof is uncertain. Completed and uncertain paths
are disjoint. Failures stop later work, preserve the first cause, and never
retry descriptor closes, downgrade permissions or roll back automatically.
Input format/selection/binding errors before execution are ordinary blockers
with no effects. This is ephemeral evidence for future journal integration,
not crash recovery or an atomic transaction against hostile same-UID writers.
Undetectable metadata-equivalent roundtrips remain outside the observation
contract. Supported stored bits do not claim execution semantics on nosuid/noexec
mounts, ACL/xattr preservation, or arbitrary user-namespace identity portability.

## Evidence and proof map

The attribute model/order follows the installed Etckeeper 1.18.20-1 evaluation
in [metadata-evaluation.md](../../dev/virtualization/metadata-evaluation.md).
No upstream executable script or GPL implementation is copied. Python 3.12
[descriptor APIs](https://docs.python.org/3.12/library/os.html), Linux
[open](https://man7.org/linux/man-pages/man2/open.2.html),
[chown](https://man7.org/linux/man-pages/man2/chown.2.html),
[chmod](https://man7.org/linux/man-pages/man2/chmod.2.html) and
[JSON compliance](https://docs.python.org/3.12/library/json.html#standard-compliance-and-interoperability)
provide the syscall/parser boundaries. Git 2.39.5's
[comparable setgitperms hook](https://github.com/git/git/blob/v2.39.5/contrib/hooks/setgitperms.perl)
confirms numeric identities/full modes/required parents but its shell/line format
and chmod-before-chown ordering are unsuitable. The implementation uses the
existing NativeGit and Policy contracts, without modifying their seams.

`tests/test_native_metadata.py` covers strict format/tree validation (R1), streamed
capture mutation (R2), isolated selected rows/shared parent review (R3), atomic
path and structural conflicts (R4), actual private index and baseline failures
(R5), reviewed route/type/link/mount fences (R6), exact owner/full mode restoration
(R7), failure/interrupt/double-fault accounting (R8), and dormancy/historical
transport/low descriptor limits (R9). Root Linux proofs physically remove files
and both parents, revert tracked history, restore only fetched Git content and
manifest, and independently stat numerical identities/all bits. Independent
bare remote, no-hardlinks destination and fresh clone prove same-kernel numeric
transport without old inode/recovery-sidecar inputs. Test oracles are assertions
only. The existing completed ext4 prerequisite is reused; generic root-capable
Linux `/tmp` qualifies stored ownership/bits here. Host Linux skips do not count
as that evidence. See [TESTING.md](../../TESTING.md) for focused commands.

The dormant Save peer also reuses `select` and `stage_candidate` (step 6.10).
Its fresh manifest is a complete remote-plus-selected hybrid, not a new complete
live snapshot. `stage_candidate(..., admission=...)` requires the capability
stored on that exact issued private index and passes its same operation/owner to
the generated manifest stream. The default `None` retains the original standalone
primitive contract. This adds no format change, Apply restoration or production
entrypoint. See [native Save](native-save.md).
