# Dormant selected native Save candidate

Step 6.10 adds `app/native_save.py`. No production module imports it. Save,
Apply, dispatch, startup, UI, backups, scope state and refs remain unchanged.
`NativeSave(preview, disk)` performs no IO on construction. Only an explicit
`build(review)` constructs content-addressed objects in a disposable or
separately authorized native repository; there is no commit, publication,
network, checkout, materialized worktree or live-write API.

## Exact selection and authority

The input must be the same current ready Save Review returned by this
NativePreview instance, with a nonempty selection. Copies, forged/reconstructed
Reviews, Apply reviews, other sessions and reset generations fail. The existing
nonblocking session lock remains held across observation, resources, tree audit
and teardown. Reentrant reset/build/select/review fails Busy.

Candidate construction uses the existing freshly observed source's exact
remote ref/OID and separately resolves its commit or tree to the full baseline
tree. The complete remote tree and fixed metadata manifest must be safe and
available. No baseline is inferred from live defaults or canonical staging.
Remote/base evidence, reachability/ref freshness, scope revision/exclusion,
policy/root/topology and selected states are checked before and after effects.
Freshness remains the source observer's orchestration evidence; local ref
identity alone does not prove a network refresh.

After an issued Review, the Save-specific fence reads only selected live file
bytes/full supported attributes or independently guarded absence, and necessary
route/parent evidence. It does not invoke Preview's whole-scope digest or
inventory. Unselected live edits/additions/deletions, including those during
selected streaming, are left outside this candidate and offered by a subsequent
fresh Preview. Necessary inode, type, mount, mode, UID/GID or selected byte
changes invalidate the review. Sibling-induced directory mtime/size/link-count
changes do not. Ordinary complete Preview/review behavior is unchanged.
Selected stream reads never exceed the remaining reviewed size. Descriptor
attributes are checked before each read and before yielding bytes; a one-byte
EOF probe is never written to a sink. Growth fails before any excess spool
write, including first/last-chunk growth, without enlarging the issued Plan.
Preview's adjacent live read/sink path enforces the same captured-size bound.

The exact tree is remote R overlaid with selected reviewed whole-file states S.
Selected deletes remove only tracked leaves and orphan manifest directory rows;
live files/directories are never removed. Every unselected blob/OID/mode and
metadata row retains R. Necessary new/changed parent metadata requires existing
explicit parent review; every unapproved existing parent is retained from R.
Leafless directory attributes use the same observed route evidence as Preview.
Reviewed parent absence is independently fenced through guarded ancestors;
acquisition errors are never absence. An approval for a pruned parent remains
in issued provenance but supplies no final directory row. If an absent live
parent still has unselected remote dependents, its remote attributes survive.
The complete manifest is formed from this hybrid, not from a full fresh live
capture. `.gitignore` and the fixed repository-only manifest exception retain
the existing metadata rules.

For A/B/C, remote A2/B2/C1 and live A2/B1/C2 selecting C yields A2/B2/C2.
Live B1 remains and a later fresh Apply Preview offers B. An independent Save
Review explicitly selecting B may replace B2 with B1.

## Resources and failures

One real NativeDisk snapshot and one `project('candidate', ...)` precede any
candidate effect. The exact recipe includes descriptor-qualified selected
present Sources, proved selected deletions, the canonical generated manifest
bytes, complete remote/final layouts and operation/owner. There are no dummy
commit/ref/reflog, external durable candidate ledger or guessed sizes. Unknown
accounting/profile/capacity and projected byte/inode shortages refuse before
creating the admission lock. A fitting operation below 500 MiB is advisory.

One issuer-registered active Admission binds the exact disk/root/executor,
operation/owner and NativeGit instance. Reconstructed/copied/inactive Admissions,
foreign owners and swapped Plans grant nothing. This same capability is held on
the issued index and propagated through `stage_candidate` to the generated
manifest spool. Normal resource intent/lease/root/owner publication is followed
by an immediate owner receipt outside the journal critical section and before
read-tree. Each empty O_EXCL spool is acknowledged and receipted before producer
bytes; completed streams are flushed/fsynced/captured and checked before
hash-object. Index/tree effects are fenced before and after acknowledgement.

Selected transfers use no-follow/nonblocking descriptors, 64 KiB chunks, exact
size/OID/full metadata and namespace checks. Already verified blob objects are
reused without hash-object freshening a frozen dependency. Metadata-only
identical index entries preserve valid unselected cache trees. The complete
returned tree is independently compared to a precomputed remote-plus-S map;
canonical manifest bytes and every record are checked independently.

Phase checks retain the initial floor/reserve and never snapshot, reproject,
scan storage, reacquire capacity or enumerate all objects. The forecast covers
selected and manifest spools/blobs/fanouts, every reachable v2 initial/removal/
final index layout (the maximum, plus full index.lock overlap), tree/subtrees,
leases/markers/journal/atomic/failure reserve. No old object is pruned. Git pins
v2 indexes, one index thread, default loose zlib compression and disabled
split/sparse/untracked/fsmonitor/automatic-maintenance extras; unsupported local
configuration refuses, ambient Git variables/global settings are sanitized.

Partial reads/writes, ENOSPC/EDQUOT/EIO, exceptions/interruption, failed
fsync/close/child, stale sources, object corruption, ownership loss, tree/manifest
mismatch and cleanup debt yield no Candidate. Primary failures survive secondary
cleanup errors with resource/fd/path addresses; uncertain/foreign resources are
retained for their owning recovery contract. There is no automatic retry,
reforecast or prune. Result return follows successful private resource/guard
teardown and the final selected/source fence.

This is a cooperative writer boundary, not a hostile same-user filesystem
sandbox. NativeDisk's documented pre-first-observation descriptor-history
limitation remains; canonical lock/resource receipts cannot prove earlier
unobserved substitutions. A qualified forecast cannot prevent external disk
consumption or subsequent kernel IO failure.

## Typed handoff and lifecycle

The frozen `Candidate` contains the exact tree/object format, observed remote
ref/OID/base/scope identity and validated original `commit`/`tree` kind, resolved baseline tree, immutable policy/root/mount
binding, snapshot/generation, sorted lossless byte selections with before/after
OID/full metadata/absence, exact `issued_parent_reviews` (before/after, all
disclosed dependents and issues) separately from final Git `reviewed_parents`,
parent dependencies/routes, canonical
manifest OID/digest, selection digest and candidate digest. Nested sequences are
captured as tuples. Both parent sets and the original source kind are bound into
the digests. There is no serialization/replay API or persisted approval.
A Candidate is diagnostic identity and is never accepted as a new Review.

Refresh, restart, update and a new Preview session clear selection authority.
No constructor/import/reopen allocates, replays, resumes or clears debt. Git
objects are durable content-addressed bytes but are unreferenced and may be
GC'd; this step does not promise reboot-safe object reachability.

Step 6.11 now uses the separately dormant [NativePublication](native-publication.md)
with this exact live issuance receipt. It revalidates complete objects, pins one
exact commit/intent and proves remote publication before local expected-old CAS.
Candidate construction remains separate and does not itself publish or persist consent.

## Proofs

`test_native_save.py` covers A/B/C and explicit B, complete SHA-1/SHA-256 trees,
raw/case/Unicode byte names, binary content, modes/UID/GID, additions/deletions,
parent closure, unselected churn, stale/foreign/reset/reentrant authority,
canonical staging/locks, one-snapshot receipts, exact index profile/physical peak,
manifest and deletion-heavy demand, capacity/config refusal, stream/object/tree/
owner faults and addressed cleanup. Existing exact NativeGit/metadata/disk/Preview
regressions cover the reused primitives. Operational admission proof requires
the local Linux wrapper; APFS refusal/host skips are not a bypass.
