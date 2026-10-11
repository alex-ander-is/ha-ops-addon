# Dormant native Git adapter: step 4

`app/native_git.py` supplies local plumbing for future native-tree callers. No
production module imports it. It does not start migration, Preview, Save, Apply,
maintenance or recovery. Existing workflows remain independent. Import and
construction perform filesystem validation only; they launch no Git process,
create no storage, and never resume a recorded operation. Refresh, restart and
version update do not clear debt or restore authority. A caller must explicitly
open a new resource or request addressed recovery.

## Capability and routing

`NativeGit(root, timeout=30, policy=Policy())` uses exactly
`root/data/native/repository.git` and `root/data/native/git-resources`. The root
can be a disposable ordinary directory; production mount/topology authorization
is still the caller's responsibility. Optional `metadata` and `resources`
arguments must equal these anchors. Positive finite timeouts are required.
There is no public executor, custom environment, alternate index destination,
repository discovery, shell command or live-file write API.

All subprocesses specify Git directory, working tree and cwd. Inherited `GIT_*`
variables are removed. Global/system config and attributes, replacements, lazy
fetch, hooks, fsmonitor, automatic maintenance, split/untracked caches and
submodule recursion are disabled. Network protocols are forbidden. Local
includes, worktree config and any configured filter are refused before
content-sensitive commands. Repository `.gitattributes` cannot authorize a
filter. Metadata routing must contain directories and single-link regular files,
with no symlink, alternate object store or split-index dependency.

Routing checks inspect anchor components, main metadata files, up to 256 loose
object fanout directories, pack/info headers, current ref/reflog routes and
linked-worktree admin routes. They do not enumerate historical loose-object
files or read historical object contents. The exact loose leaf is checked before
blob IO. Tree-dependent commands also inspect the supplied tree's reachable tree
routes and referenced object leaves; this costs traversal of that current tree,
not all historical trees. Resource integrity checks read only current recorded
content. Materialization publishes changed file evidence incrementally, avoiding
reading previously materialized blobs after every file.

Ref observations additionally stream the whole current `packed-refs` file and
validate every record's framing, direct name, nonzero full OID, object route/type,
optional peel and declared sorted order. This costs current packed-ref records
and their referenced object headers, without enumerating object history.

This is a private cooperative-writer boundary. No-follow descriptors, identity
checks and CAS protect against unsafe pre-existing routes and cooperative
concurrency. They do not provide an atomic filesystem sandbox against a hostile
same-user process replacing routes during a command.

## Byte records, objects and refs

| API | Contract |
| --- | --- |
| `status(untracked_paths=())` | Complete porcelain-v2 `-z` records, including rename partner and unmerged stages. Default excludes untracked discovery. Explicit untracked paths must be observed regular files and are literal pathspecs. |
| `index_entries(handle=None)` | Entire stage/mode/OID/path index with `--stage -z --sparse`. Canonical reads disable optional locks. |
| `tree_entries(full_oid)` | Complete recursive tree records from an exact commit or tree OID. |
| `read_ref(full_ref)` | Direct qualified `refs/` name and exact OID or proven absence. Observation errors are failures. Symbolic/pseudo refs are refused. |
| `update_ref(name, new_oid, *, expected_old)` | Git `update-ref --no-deref` CAS. Required explicit `None` means expected absence, encoded with the object format's zero OID. |
| `delete_ref(name, *, expected_old)` | CAS deletion requires a full expected OID. |
| `copy_blob(full_blob_oid, binary_sink)` | Stream at most 64 KiB per transfer, handle short sink writes, independently verify streamed size/hash, and return `BlobResult(oid, size)`. |
| `write_blob_stream(reader_or_chunks)` | At most 64 KiB reader requests and chunk views; an owned spool bounds adapter memory. No filters or newline conversion. Return measured size and verified full OID. Bytes convenience retains the caller's proportional-memory input. |

Optional untracked reporting uses a separate literal scoped query; it never
narrows the complete tracked status query. Only explicitly requested untracked
records are appended. Ordinary and rename/copy status axes follow Git's
independent HEAD/index and index/worktree collectors, including real
intent-to-add `1 DA` and removed intent-to-add `1 DD`. The latter requires a
zero worktree mode; the seven conflicts use the explicit `u` stage shape.
Rename/copy records require a matching score kind and paired original path;
incompatible status fields are malformed output.

Paths remain Unix bytes; display strings cannot authorize selections. NUL
framing, fixed fields, mode/stage/OID grammar and rename pairing are validated;
malformed/truncated output never returns a partial successful list. SHA-1 and
SHA-256 full lowercase OIDs are supported. Blob reads refuse trees, commits,
annotated tags and abbreviated/revision identities. A failed sink may have
received partial bytes; there is no successful result until the child exits and
size/hash validation completes. Every child has a deadline and is reaped;
stderr is drained separately with a 64 KiB retained diagnostic bound.

Ref timeout/interruption raises `RefUncertain` with before, reobserved after (or
unavailable) and cause. It never repeats the mutation. CAS failures are
`RefConflict` when fresh observation proves a changed expectation. Local
loose refs must contain exactly one full nonzero OID with at most a final LF;
empty, malformed or unusable existing refs cannot be reported as absent.
Corrupt packed records, missing packed objects, diagnostics or unexpected quiet
stdout are failures even when Git returns a usual success/absence code. Valid
packed/peeled refs and loose overrides remain observable; a name present in
validated packed storage cannot silently become absence. Local
ref/blob writes are explicit plumbing effects, not publication authority.
Unreachable objects may remain; resource cleanup never removes Git objects.
Before an object writer can reuse/freshen an existing target, the adapter checks
its exact route and streams its stored type, size and content-addressed hash.
It verifies the stored result again after writing. A correct producer digest or
Git stdout alone is insufficient: corrupt existing objects are refused and
preserved. For `write_tree`, canonical raw tree bytes compute every root/subtree
OID from the complete audited private index before Git runs. Every generated
destination is checked, including symlink/hardlink leaves; result root identity
and all stored subtree hashes are verified afterward. This traverses only the
candidate, without scanning historical loose objects.

Immediately before `write_tree`, every blob OID in that complete index receives
fresh route and exact type validation, including unchanged and unselected base
entries. Prior successful index updates do not make object storage immutable;
an unsafe/missing/wrong-type dependency blocks Git's tree effect.

## Private indexes and worktrees

`isolated_index(base_tree_oid_or_None, operation_id=UUID, owner_id=UUID)` yields
an opaque resource handle. `None` explicitly selects an empty index. `read-tree`
seeds the complete supplied tree, never copies/swaps the canonical index and
never updates live files. Every index command routes `GIT_INDEX_FILE` to this
resource, including failure paths. Every public consumer/mutator first rechecks
the complete journal-bound index identity, mode, content hash and resource
snapshot. Absence is allowed only during initial allocation before `read-tree`;
a missing acknowledged index cannot become an implicit empty base. Changed,
replaced or unknown entries, including an existing unacknowledged `index.lock`,
are refused before index-consuming Git commands. Index operations serialize
validation, effect and acknowledgement within the adapter. Only a successful
prevalidated command can acknowledge its own index changes. Failed/uncertain
commands retain the previous evidence and any differing result as cleanup debt,
instead of adopting the current filename contents.

`update_index(handle, IndexEntry records, removals=exact_paths)` accepts only
regular `100644`/`100755`, stage zero, exact blob OIDs and policy-eligible paths.
It uses NUL index-info records rather than `add`, `rm` or broad pathspecs.
`write_tree(handle, allowed_paths=complete_exact_whitelist)` audits the entire
candidate, including unselected base entries: conflicts, symlinks, gitlinks,
sparse directories, duplicates, known secrets and paths outside that whitelist
are blockers. The full allowed candidate set differs from the selected mutation
set, so unrelated base entries retain their exact OIDs.

`temporary_worktree(full_commit_oid, operation_id=UUID, owner_id=UUID,
paths=exact_paths)` uses detached `worktree add --no-checkout --lock` with a
resource-bound reason. Empty paths materialize nothing. Selected regular blobs
are exclusively reserved with each leaf descriptor closed immediately, then
streamed sequentially through descriptor-relative verified no-follow reopens
with their executable meaning preserved. Open descriptors stay bounded in
selection size and path depth; file bytes are never accumulated in memory.
The complete path/type/parent set is validated first; all filenames are reserved
before content writes, detecting case or
normalization collisions on filesystems unable to represent the requested set.
Parent reservations require the same exact byte prefix and directory inode;
different parent spellings cannot alias even when their leaf names differ.
Traversal, absolute/NUL paths, `.git`, duplicates, parent collisions and unsafe
parents are refused. Reopens use neither create nor truncate and use nonblocking
mode to refuse substituted FIFOs without hanging. Root and parent identity/mode,
plus each regular single-link leaf's identity/mode/empty size and nanosecond
mtime/ctime, must match both before open and on the opened descriptor before
copying. Writes preserve the descriptor's observed identity; publication records
cached original reservation/write evidence, never a refused current occupant.
Completed content is hashed once for publication, with inexpensive metadata
checks for earlier files rather than repeated whole-tree content reads.

Observed namespace/identity/mutation refusals retain addressed worktree debt;
automatic context cleanup retains an observable same-inode edit truncated back
to its former empty content when the compared metadata changed. The resource
owner resolves foreign changes and retries that exact resource ID; recovery
still requires the existing complete
identity/mode/digest and admin proofs. A producer error or interruption remains
the primary error, with publication/cleanup/descriptor-close failures appended.
Descriptor ownership transfers before closing predecessors, and uncertain close
errors never trigger a second close attempt on the same descriptor number.
Owned-content and baseline marker/backlink verification use a separate worktree
readback path with the same single-owner close scopes, no-follow/nonblocking
reads, pre/post-read metadata checks and exact streamed marker-byte validation.
Readback parent traversal transfers successors before predecessor close, so an
uncertain close cannot strand the successor or retry a closed descriptor number.
Materialization also uses separate journal loading/release and publication
scopes. Manifest reads, lock acquisition/unlock/close, raw publication descriptors,
stream transfers, file/directory durability and temporary-file removal preserve
the first exact exception and append later failures. A collected verification
error already counts as the primary error before later durability/publication;
checked publication may still retain the original proven ownership as debt.
Unlock errors still reach descriptor close. Temporary publication removal
requires the original observed identity; if identity was never obtained or the
pathname was replaced, the temporary occupant is retained rather than adopted.
Generic private index/spool record/capture/journal/publication helpers and the
journal schema retain their existing behavior.

These checks cover substitutions observed at the reservation, reopen and
publication boundaries within the existing private 0700 resource/cooperative
lease model. Timestamps are change evidence, not unforgeable provenance. A
same-inode grow/truncate roundtrip can leave every compared field identical,
including natural same-clock-tick timestamps. Such metadata-equivalent mutations
are indistinguishable at these boundaries and may proceed normally; they do not
establish a detectable ownership refusal or cleanup debt. The checks do not
provide atomic exclusion of arbitrary same-UID mutation or ancestor
renames after the final checks. No Git checkout/filter fallback exists.

LF and CR names remain diagnostic identities in status/index/tree records.
**Every selection entrypoint refuses `unsupported-line-break-name`**, including
removals, materialization and the complete allowed-path set. No index/resource
mutation follows such refusal.

`worktree_path(handle)` exposes the issued private tree only while the context
is active. Foreign additions or edits prevent cleanup; exposing this path does
not grant authority to remove user files. Constructed, copied, closed, wrong
repository/operation/owner handles are rejected before Git.

## Durable ownership and recovery

The independently versioned schema-1 journal does not change
`NativeOperationStore`. It binds root, metadata and storage identities, UUIDs,
kind/phase, ownership marker and lease identities, lossless relative byte paths,
file/directory evidence, and exact worktree admin/backlinks/lock token.
Directories are private 0700; journal/marker/lease files are 0600. Strict JSON
rejects duplicate keys, nonfinite numbers, malformed identities and unknown
schema fields. Files and directories are fsynced before durable references;
journal replacement is atomic and followed by a parent-directory fsync.

A stable journal `flock` plus process mutex serializes all record mutation. A
per-resource kernel lease spans the entire active context. Recovery first takes
the stable journal lock, validates the existing lease inode, then tries its lock
without blocking. Active resources return `busy`. Lease unlink, absence checks
and journal publication share that stable journal lock; another cooperating
recoverer cannot open a replacement lease during this terminal transition.
Nested contexts and repeated owner UUIDs receive independent resource UUIDs.

Allocation intent precedes creation. Records distinguish intent, allocated,
creating, ready and debt. A crash releases the kernel lease but retains the
record. Fully registered interrupted worktrees can be recovered only after
exact ownership token and two-way admin backlink proof. A remover killed after
its effect can be acknowledged only when both exact tree and admin are absent.
Unacknowledged index/spool content, missing/replaced markers, uncertain partial
registration or foreign content stays explicit debt; recovery never guesses
ownership. Such refusal requires the fixture/resource owner to resolve the
reported ambiguity before another addressed retry.

`recover_resource(resource_id, owner_id=UUID, operation_id=UUID)` handles exactly
one record, returning `removed`, `absent`, `busy` or `debt` with all cleanup
errors. There is no sweep, global prune/repair, automatic takeover or replay.
Unknown files, replaced roots, symlinks, FIFOs, hardlinks and Linux mount
boundaries (including same-device mounts) are preserved. Forced Git removal is
allowed only after the complete tree and admin snapshots prove every entry.
Independent known files are attempted even after another cleanup failure.
Original operation errors retain their `cleanup_errors`; successful operations
with cleanup debt raise `CleanupDebt`. Failed absence publication retains a
retryable record, even if the exact resource is already gone.

`resources()` exposes active/unresolved records; `accounting()` reports active
kernel leases, unresolved records, actual safely observable temporary/admin
bytes and observation errors. It includes unacknowledged in-flight regular
content, without traversing completed history. Verified completed records and
their lease files are removed. Stable journal storage remains. Numeric quotas,
disk admission/reserves, eviction and whole-App accounting belong to step 5.
Host macOS checks do not establish Linux mount or full byte-filename behavior;
the required local Linux wrapper does, without privileged/live mounts.

## Acceptance and risk proof map

All new executable proofs are in `tests/test_native_git.py`.

| Obligation | Executable evidence |
| --- | --- |
| A1 / R03, R06 | Kind-specific XY/NUL parsers; complete tracked status plus exact optional untracked requests; real rename/conflict/stage records; full Linux unusual-byte index/tree/materialization fixtures; LF/CR diagnostic and all selection refusals; SHA-1/SHA-256 binary streaming, short writes, object type/hash corruption on read and existing-object reuse/write, generated tree root/subtree destination/content checks, and timeout/reaping. |
| A2 / R04 | Required expected-old, explicit absence, stale/correct update/delete, packed/peeled/loose-override and symbolic refs, both-format corrupt packed records/names/OIDs/peels/order and diagnostic refusal, separate-process one-winner CAS, observation failure and effect-after-timeout evidence without replay. |
| A3 / R01, R02, R05 | Canonical index/HEAD/live snapshots; complete independent base overlay; unsupported complete-index modes, missing/edited/replaced acknowledged indexes, unknown locks and failed-effect evidence retention; current selected/unselected blob dependency routes/type/missing guards and generated subtree destinations; config/filter/include/ambient environment, hooks/fsmonitor, replacement and no-fetch guards; inspected argv/cwd/index routing. |
| A4 / R07–R11 | Real process deaths at allocation/ready/removal boundaries and after worktree registration; unacknowledged index/spool debt; active lease/busy and concurrent/nested allocation; forged/closed handles; foreign content/marker/special-file/mount preservation, parent-component alias refusal before content; independent cleanup errors, fsync/absence publication debt, addressed retries. |
| Materialization descriptor/ownership regression | Real subprocess with 120 mixed-mode files and nested/shared/distinct parents under soft `RLIMIT_NOFILE=64`; every reservation empty at first copy; descriptor balance for success/open/fstat/fdopen/copy/interrupt/flush/chmod/fsync/publication failures; leaf/root/parent/repeated-prefix and stat/open substitution refusal; regular/symlink/FIFO/hardlink/growth/truncate/chmod/unknown-addition non-adoption, retained addressed debt and exact retry; linear blob hash-read count; deterministic changed-timestamp roundtrip fixtures and first-error preservation across writer/root/parent/next-parent/leaf close double faults, including interrupts and transfer-before-close without retry; actual readback syscalls for selected/marker/backlink evidence, with single predecessor-close and file-read/fstat/close or parent-open/fstat/final-close failures, exact first-error identity, zero FD delta and addressed recovery; actual materialization journal manifest/lock and publication acquisition/read/write/flush/fsync/transfer/release/unlink faults, collected baseline failures plus later durability/publication failures, parser and interruption triple-release failures, and refusal to remove substituted publication occupants. |
| A5 / R12 | Constructor/restart/update dormancy; production AST import guard; accounting and completed-record removal; Linux proof and adjacent native inventory/state plus repository suite. |

Git behavior was checked against the pinned 2.39.5 local runner and tagged
upstream documentation: [environment and routing](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/git.txt),
[update-index](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/git-update-index.txt),
[update-ref CAS](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/git-update-ref.txt),
[worktrees](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/git-worktree.txt),
[exact blob reads](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/git-cat-file.txt),
[porcelain-v2 status grammar](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/git-status.txt),
[independent status collectors](https://raw.githubusercontent.com/git/git/v2.39.5/wt-status.c),
[packed-ref readers](https://raw.githubusercontent.com/git/git/v2.39.5/refs/packed-backend.c),
[existing-object reuse/freshening](https://raw.githubusercontent.com/git/git/v2.39.5/object-file.c),
[Git storage routes](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/gitrepository-layout.txt),
and [lazy-fetch suppression implementation](https://raw.githubusercontent.com/git/git/v2.39.5/promisor-remote.c).
No new dependency or production runtime activation is introduced.

## Dormant selected Save qualification (step 6.10)

`resolve_tree(source_oid)` verifies the raw exact local commit/tree identity,
resolves its tree header and validates the full tree before returning a tree OID.
`resolve_tree_source(source_oid)` performs the same validation and returns the
original `commit`/`tree` kind alongside the resolved tree OID; Save captures
both without inferring a publication parent from a direct tree.
The exact streaming verifier admits commit objects internally for this seam.

The optional `admission` on `isolated_index`, and explicit
`operation_id`/`owner_id`/`admission` on `write_blob_stream`, accept only an actual
issuer-registered active candidate Admission. The issued index stores that exact
capability; normal owner graph publication precedes its receipt outside the
journal lock, before read-tree. Empty spool O_EXCL publication/receipt precedes
content. Completed spool, index and tree effects use the same frozen authority.
No caller callback/boolean or copied capability grants resource ownership.
Unqualified existing callers retain their separate resource contract.

Commands now pin `index.version=2`, `index.threads=1`, `index.sparse=false` and
default `core.compression=-1`/`core.looseCompression=-1`, in addition to existing
sanitization. The candidate's NativeDisk profile refuses unsupported local
settings before resources. Qualified verified existing blobs are reused without
Git timestamp freshening; identical selected index entries preserve cache-tree
validity for metadata-only changes. Spool/lease close failures preserve original
exceptions plus addressed debt. See [native Save](native-save.md).


## Dormant exact publication primitives

NativeGit remains local-only. Exact raw commit verification/write, targeted object/ref durability and ref-lock pin deletion require the issued publication Admission. A bounded publication-local context validates controls once and rechecks captured route/descriptor identities thereafter. The separately dormant NativePublication owns all outgoing CLI transport; see [publication contract](native-publication.md).
