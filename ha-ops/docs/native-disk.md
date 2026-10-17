# Dormant native disk accounting and admission

`app/native_disk.py` performs one initial storage calculation and a qualified
forecast for an operation. It has no production import or executor. Construction
performs no I/O; `snapshot()` observes storage; `guard()` creates only a private
stable admission lock. It never applies configuration, starts a worker, creates
system backups or cleans storage.

## Initial accounting and API

Private anchors are `data/native` and `data/native-operations`. Categories are
Git (`repository.git`), staging (`git-resources` and opt-in private legacy
copies), diff (`native/diffs`, operation artifacts), recovery (`native/recovery`)
and service/control files. Stale, orphan, temporary, cleanup-debt and directory
allocations remain counted. `Layout(legacy=...)` accepts explicit private copies
such as `data/repo`; original live roots are excluded.

System backups are user-managed. There is no backup category, recipe, receipt,
constructor input or Evidence parameter. No system-backup root is scanned or
attributed. Their consumption still reduces actual destination-volume
`f_bavail`; before-write recovery copies and encoded recovery metadata remain
part of the forecast. The active legacy backup workflow is unchanged.

Logical regular-file length and physical `512 * st_blocks` remain separate.
Distinct `(st_dev, st_ino)` allocations count once across hardlinks/overlapping
anchors. Unmatched hardlink counts are uncertain ownership. Directory blocks
count physically; shared/reflink/compressed extents may be overcounted.

A snapshot uses two bounded coherence passes within one acquisition. Byte paths,
no-follow components, incremental descriptor-relative scanning and a bounded
open-descriptor set prevent unsafe traversal. Stat/open errors, unsafe objects,
changed membership/identity and exhausted entry/time budgets are incomplete
results. Incomplete accounting launches no Git content observations. There is
no automatic retry loop. `Limits.seconds` defaults to 20 seconds as an
observation budget, never a permit lifetime or file-size limit; callers may
choose larger valid work budgets.

Actual capacities come from held destination descriptors: `f_bavail *
f_frsize`, available inodes when exposed and Linux mount identities. Missing
outputs use their real existing parent. Bind aliases of one device/filesystem/
source share conservative minimum capacity and summed demand. Stacked,
readonly, quota and unsupported routes cannot claim admission.

`source(path)` captures a single-link regular descriptor observation.
`selection(...)` binds a complete verified Inventory. Immutable Evidence captures
nested sequences/buffers, operation/owner, selected sources, complete base tree,
exact prospective controls, diff/commit/ref/reflog bytes, incoming pack/index,
independent recovery copies, replacement blobs and encoded metadata. `Target`
distinguishes exact files from generated-output directories. Caller numerical
peak/reserve/remainder values are never inputs.

`Control(target, payload, operation_id=op, owner_id=owner)` keeps owning JSON
bytes unchanged and binds identity separately. Canonical active state can derive
identity from its worker. Generic controls may embed identity. Strict UTF-8 JSON
uses the canonical owner parser; duplicates (including nested/escaped aliases),
nonfinite values, exponent overflow and malformed data refuse.

Only actual same-instance issued Snapshots and Plans carry authority. Recreated,
edited, serialized or cross-instance reports refuse before Git compilation.
A private context retains the full immutable snapshot/forecast, dependencies and
initial artifact proofs. Unreferenced diagnostic plans release their contexts.

The `git-native-disk-v1` executor contract requires:

- Default Git zlib window/memory parameters and no compression override;
  the loose bound includes `type length\0` before applying
  `Z(n) = n + (n >> 12) + (n >> 14) + (n >> 25) + 13`.
- Complete stage-zero regular SHA-1/SHA-256 entries, index version 2, no split,
  sparse, fsmonitor, untracked, EOIE or parallel IEOT output. Generated TREE
  extension demand is included. Future executors must pin this profile.
- Repack uses complete all-object enumeration, including unreachable objects,
  with no delta/reuse, bitmap, MIDX, commit-graph or other maintenance outputs.
  Old objects/packs remain in the baseline until actually removed.
- Exact full control serialization is supplied by the owning future executor,
  bound to operation/owner. It includes the actual pending state/failure
  envelope rather than a small summary. Copies/metadata/diff/commit artifacts
  are independently captured bytes, not claimed sizes.

Read-only Git observations use NativeGit's pinned environment and command
options: fixed repository/worktree, sanitized Git variables, system executable
path, no replacements, lazy fetch, prompts, global/system attributes/config,
optional locks, hooks, fsmonitor, sparse/split/untracked cache, automatic
maintenance, external diff, protocols or submodule recursion. Output/deadlines
and records are bounded. Replacement refs cannot understate Apply or worktree
output: both observer and NativeGit resolve the original object. Adding,
changing or removing an ignored replacement ref does not invalidate a selection
whose actual output remains unchanged. Unsupported config/routing is explicit unknown;
ordinary `remote.*.url/fetch/pushurl` and `branch.*.remote/merge/pushremote`
routing are accepted without performing any network operation. Promisor,
compression/index overrides and execution-capable configuration are rejected.
incomplete snapshots launch no Git content observations. No adapter writes
or existing mutating accounting APIs are called.

| Kind | Accounted concurrent outputs / evidence limit |
| --- | --- |
| Initial selection/scope/Save | Full logical stream spool and worst loose blob, fanouts, complete base/final index plus index.lock, every generated parent tree, optional materialization, exact diff and Save commit/ref/reflog outputs. Save requires a qualified `Evidence.ref` and exact `reflog_entry` serialization; its actual ref/log routes, deep missing parents, existing full log append/copy and atomic ref/log overlap are projected |
| Migration | Same candidate resources plus each explicitly measured private metadata/history copy; entire legacy baseline remains counted |
| Apply | Each independent before/recovery copy, exact local Git replacement-blob size, replacement temporary, measured encoded metadata artifacts and full controls; absent metadata adapter is unknown |
| Local repack | Complete local blob/tree/commit/tag sizes; no-delta pack/header/index/temporary overlap, with old packs retained |
| Fetch with standalone local payload | Descriptor-verified paired pack/index and `verify-pack -v` checksum/object proof; spool, copied complete pack/index and index replacement. This route imports a standalone pack, so thin repair is already resolved |
| Ordinary remote fetch | Estimated local baseline, explicit unbounded incoming/repair dependency; old local pack bytes never authorize future remote transfer |

There is no transfer engine or metadata adapter.
An estimated recipe is useful diagnostics and returns
`cannot-establish-admission`, distinct from a proved shortfall.

## Allocation profile and reserve

Ordinary writable Linux ext4 and tmpfs have explicit conservative operational
profiles. Quota flags, remote/stacked/unknown filesystems and readonly routes
cannot claim the same profile. Each output rounds data to measured `f_frsize`,
includes a separate directory-entry block and each missing parent inode/block.
Ext4 additionally budgets one metadata block per data block plus two
extent/admin blocks per file. This deliberately overestimates sequential
ordinary writes rather than assuming directory/extent metadata is free.
Known encoded ACL/xattr/recovery payloads belong to the recipe.

`f_frsize` is an accounting quantum, not a universal filesystem allocation
theorem. The profile is an operational projection for the stated executor,
with visible terms and allocation regressions. Unexpected kernel, quota,
unmodeled maintenance or external-writer growth remains an executor failure
and recovery concern. It does not promise that all future filesystems fit
these terms.

For each involved volume the measured reserve is:

```text
round_Q(largest current atomic control)
+ round_Q(full exact pending/failure control)
+ Q
+ observed comparable growth
```

Current and pending atomic copies also remain visible operation demand.
Only explicitly supplied completed same-instance `previous` evidence with
identical root/layout and volume/mount/allocation profiles measures
`max(0, previous_available-current_available,
current_attributed-previous_attributed)` with timestamps. Phases take no samples;
no earlier comparable evidence means no claimed growth history. This is a cushion, not a bound on
uncooperative external writers.

Admission requires `additional_peak + reserve <= available` independently per
volume and sufficient exposed free inodes. Equality fits; one-byte shortfall
does not. Unknown inode metrics are visible, never invented. Warning means
strictly less than `500 * 1024 * 1024` bytes (binary MB as in the app).
It never independently refuses a fitting operation. There is no total Git,
file-size or file-type cap.

## Operation context and phase checks

```python
snapshot = disk.snapshot()
plan = disk.project('scope', snapshot, evidence)
with disk.guard(plan) as admission:
    admission.revalidate()
```

Guard entry and every phase reuse the exact initial Plan, allocation/inode floor,
reserve and capacity decisions. They never call `snapshot`, `project`, `_scan`,
`_entries`, `_objects`, `_profile`, `_destination`, `_volume`, Git enumeration or
`fstatvfs`. Elapsed time and unrelated external storage growth do not replace the
forecast. A held operation cannot request another snapshot/project or switch to
a smaller plan. A successfully entered plan is one-shot; ended Admission cannot
resume. Explicit new operation calculation starts fresh; refresh/restart/version
repair never recreates permission. Physical leftovers enter initial accounting.

Every optional route distinguishes proved absence from acquisition failure.
A bounded nofollow walk holds each real parent, checks component identities,
and confirms a missing stat with an independent nofollow open. Existing or
unverifiable components refuse instead of becoming `None`; permission and other
I/O errors cannot supply absence. Snapshot-known files and ancestors cannot
become prospective paths between snapshot and compilation, even when they are
now genuinely missing. A failed acquisition of a snapshot-known leaf refuses
immediately, before any absence probe: later return of the same inode, with
unchanged or changed bytes, cannot erase the failure. Leaf authority and full
preimages have a different role from parent identity. A parent observed missing
during an absence probe stays disqualifying even if it subsequently returns;
valid parent identity cannot requalify a lost known leaf. This is finite refusal,
without retries, phase rescans or a replacement forecast. Config, HEAD, direct/packed refs, relevant object storage,
reflog preimages, output namespaces and owner controls use this rule. Initial
relevant storage sources are reacquired before a context is issued. These phase presence proofs use
only targeted route/source observations, without enumeration or content reads.
An initially observed or subsequently canonically validated owner control stays
presence-bound through later phases, including an empty journal after resource
cleanup. Genuinely absent initial routes and immediate new-owner receipts remain
usable; no original allocation, inode or reserve term is reduced.

Targeted checks bind source observations and complete parent routes, root and
output device/inode/mode/absence, the admission lock and relevant mountinfo
identity. Relevant loose object or immutable pack/index storage files, config, HEAD, the HEAD loose route,
explicit Save ref and packed-ref file are compiled dependencies. Direct target
OIDs use the configured width, nonzero and local-object predicates. Relevant
packed storage is validated once initially before selecting targets, including
when a loose override exists. Every line must satisfy NativeGit's configured
nonzero OID width, qualified Git name, exact LF/separator, unique name,
header/order and single peeled-record grammar. Invalid records anywhere cannot
become apparent target absence. Empty/header-only storage and valid unsorted
storage remain supported. Entry/byte/time observation limits bound this pass.

NativeGit qualifies every packed and peeled OID before reading a loose override.
The initial calculation therefore qualifies that finite explicit OID set once
with a bounded cat-file batch (reusing already proven base/output OIDs), then
retains their targeted immutable storage proofs. It does not walk unrelated
objects. Targets different from the declared base are also bound. Phases parse
no packed bytes and perform no Git/object enumeration. Observable
mutation, disappearance, loose absent-to-present change or packed-ref replacement
refuses. Phases do not read packed-ref contents or call NativeGit `read_ref` or
`_validate_metadata`; those would enumerate unrelated refs. Ordinary HEAD ->
direct branch routing (loose or packed) remains supported. A symbolic alias in
that branch or an explicit Save ref refuses initially, consistently with
NativeGit's direct-ref contract, including chained/packed/absent alias targets.
Unsupported HEAD syntax and Git alternates/includes/config profiles also refuse.

Complete Repack additionally binds initial directory membership fingerprints
(device/inode/mode/size/link count/mtime/ctime/blocks) for objects/, every existing
fanout and pack/. Targeted held-directory fstat checks detect existing/new fanout
objects, removal and new pack routing without enumeration or a second forecast.
Selection binds output/ref object routes plus every packed/peeled object needed
for canonical packed-storage validity; unrelated loose additions remain allowed. Current known object storage is checked by targeted observations, never
another all-object listing.

Exact generic output absence is bound. The future executor immediately records
planned parent/file creation through `record_directory_creation(relative, fd)`
or `record_file_creation(relative, fd)`. Held O_EXCL descriptors and exact
bytes/source-copy/blob proofs reject wrong type, route, identity or contents.
Receipts preserve every original allocation/inode/reserve term; changed receipt
observations refuse. Existing output/ref/log replacement remains unsupported for
continued phases and stops the operation; it cannot trigger a second calculation
inside the held context.

Each phase probes existing operation/journal locks without creation or waiting,
then reads canonical current state/journal and immediate resource membership.
Persisted journal and operation state require no-follow single-link regular files
with mode 0600. Resource storage requires mode 0700, and an existing journal requires a private
stable journal.lock. Both initial lock presence and identity are bound to the
actual snapshot. Snapshot/current comparisons are symmetric: an observed lock
cannot become prospective absence after a FileNotFoundError, missing parent or
loss of its control file. Missing, substituted or nonprivate locks refuse from
compilation through guard entry and every repeated phase. Operation state may initially
lack its lock, consistent with the owner's lock-creation contract. A new declared
same-operation resource/state can establish one current lock receipt while the
actual probed flock is held and its canonical graph/namespace validate. The
receipt retains device/inode/mode through later phases and never lowers demand;
the prospective resource journal lock's allocation/inode demand is included.

That first receipt proves current authority, not an unobserved historical owner
FD: the existing owner schema does not persist journal.lock identity. An
uncooperative substitution before the first receipt can conceal a lock held on
an old, never-observed inode. The future caller must take the first receipt
immediately after canonical creation, outside owner critical sections and before
further effects. Existing snapshot locks and every established receipt are
fenced against later substitution. Tests expose the pre-first-receipt limit
with a real held original flock rather than claiming historical proof.
Canonical validators remain the actual NativeOperationStore and NativeGit
validators; no second workflow schema or durable ledger is introduced. A foreign
active worker/live lease, unaddressed resource, replaced lease/root, unqualified
resource output or exceeded initial control envelope refuses.

The initial envelope allows one new index, at most one spool per selected source
and one worktree only when materialization is declared, in addition to addressed
initial resources. Full marker/lease/directories/journal, index/index.lock,
worktree data/parents/admin/backlink controls are included initially. Journal
cardinality, serialized length, entry count/kinds, exact paths, relevant identities
and declared data lengths remain bounded. Current errors/new debt or unsupported
shapes stop ordinary effects. Unchanged addressed inactive debt remains diagnostic
baseline. Canonically acknowledged same-operation NativeGit directory creation
and removal supply context-only namespace receipts; no demand is reclaimed.
Large resource content integrity remains the owning executor's responsibility.

Actual owner.json evidence uses one held no-follow single-link private regular
file: exact raw bytes and length must equal NativeGit's encoding, raw SHA-256 and
identity must equal its journal acknowledgement, and descriptor/route/root/lease
observations must remain stable. Newline, whitespace/reordering and newly
acknowledged noncanonical bytes still refuse. Worktree backlink controls are
checked by exact targeted raw evidence; no recursive `_admin_binding` mutation
helper is used.

An initially bound canonical worker may advance accepted -> effect_started under
the same instance, generation, record, marker, owner and epoch, with a compiled
full state serialization. An exact prospective canonical active control can
qualify the same transition from a preclaim idle state. Unqualified new claims,
removed/terminal/invalidated workers, recovery_required and lifecycle/epoch
changes (including same-owner takeover) stop normal effects. Recovery is the
owner's separate procedure, never permission borrowed from this guard.

Initial artifact verification is retained across project, entry and phases.
Every phase still validates the current canonical graph; unchanged historical
artifacts cause zero additional content reads. Cache keys bind digest/measured
length, complete route/parent identity and regular-file observation. Changed
leaf observations reverify under the observation budget or refuse; changed
parent routes refuse. Ended contexts clear proof. Stat-equivalent writes within
a filesystem timestamp tick remain a disclosed cooperative limitation.

The lock order for future integration is disk admission, operation state,
resource journal, resource leases. Revalidate outside owner critical sections.
Thread/process contention refuses nonblockingly; process death releases flock.
Existing NativeGit/legacy code does not acquire this disk lock. NativeGit's
write_blob_stream currently generates independent UUIDs, so activation needs a
future identity adapter. Nothing here grants execution authority rejected by
that owner or activates legacy Save/Apply.

## Failure duties and proof limits

Initial capacity/reserve is a cushion, not exclusive filesystem ownership or an
ENOSPC guarantee. Future executors must stop on short write, ENOSPC, source/CAS
failure or control publication error; acknowledge only actual completed effects,
preserve journal/recovery/cleanup-debt evidence and report uncertainty. A refreshed
calculation or automatic retry cannot make an incomplete effect successful.

Existing isolated NativeGit short-sink, ownership/publication/materialization
failure and cleanup-debt tests, and NativeOperationStore manifest fsync/replace/
artifact failure tests provide the unchanged owner evidence. No future live
executor or metadata adapter is invented for this dormant step.

Tests cover one initial forecast with forbidden phase scanners/capacity/Git,
immutable floor and one-shot lifetime, forged Snapshot/Plan and context GC,
source/ref/config/object changes and acquisition-error/known-loss matrices at
initial compilation, guard entry and repeated phases, refused symbolic aliases, pinned replacement
semantics, Repack membership/pack routing, private journal/state and stable owner
locks (including real held inode plus lock/control/parent loss), complete packed
structural grammar and batched packed/peeled object qualification, bounded real
index/spool/worktree transitions,
canonical preclaim/effect/terminal/lifecycle states, one initial historical
artifact read, exact output receipts, byte-marker mutations and parser
corruption. The AST sentinel catches flat, package-qualified and relative imports
with aliases/nested functions. Its only peer exception is observational NativeGit
imports from native_disk.py; actual production imports remain absent.

The low-space warning fixture conservatively caps initial available bytes; its
allocation measurements still use the kernel. The local Linux wrapper uses
writable tmpfs for actual allocation/free-space,
invalid byte paths, incompressible spool/blob, no-reuse repack, tiny outputs and
independent process/flock proofs. Input/output virtiofs mounts prove real bind
aliases and shared capacity, not ext4 allocation. Those step 6.5 tests used a
read-only persistent root and did not establish writable ext4 allocation.
Step 6.6 now supplies [bounded guest-native ext4 qualification](../../dev/virtualization/README.md#guest-native-ext4-qualification)
with measured held allocation/inode peaks, real byte/inode ENOSPC, scoped owner
short/EIO failures and exact fixture cleanup. Host arithmetic seams remain
separate; new allocator/output families still require their own qualified
evidence, and future Apply/rollback executor proofs remain deferred.

Future steps supply execution identity/controls, recovery metadata adapters,
Calculate/Compact and incoming/legacy migration integration. This step changes
none of those production workflows.

Evidence reviewed October 7, 2026: [Python descriptor/stat APIs](https://docs.python.org/3/library/os.html),
[Git index format](https://git-scm.com/docs/gitformat-index),
[pack format](https://git-scm.com/docs/gitformat-pack),
[all-object enumeration](https://git-scm.com/docs/git-cat-file),
[pack verification](https://git-scm.com/docs/git-verify-pack),
[repack](https://git-scm.com/docs/git-repack),
[Git 2.39.5 compression source](https://raw.githubusercontent.com/git/git/v2.39.5/zlib.c),
[zlib bound source](https://raw.githubusercontent.com/madler/zlib/v1.3.1/compress.c),
[Supervisor API](https://developers.home-assistant.io/docs/api/supervisor/endpoints/).
The maintained comparable search found [Borg free-space guidance](https://borgbackup.readthedocs.io/en/stable/quickstart.html)
and [restic repository code](https://github.com/restic/restic/blob/master/internal/repository/repository.go).
They support retaining working space and distinguishing stored/current objects
from future writes, but do not provide HA Ops/Git executor upper bounds. No
code, policy floor or dependency was copied.

## Dormant Save candidate recipe (step 6.10)

`project('candidate', snapshot, Evidence(...))` is distinct from publication Save.
It requires exact `candidate_manifest` bytes, complete `candidate_final` paths,
selected descriptor Sources and independent selected `candidate_removals`
absence. It excludes commits/refs/reflogs, materialization, external controls,
recovery and network artifacts. Its only durable control envelope is derived
from the actual bounded NativeGit journal/owner contract; no dummy ledger exists.
The generated manifest adds one qualified spool/blob/fanout to the selected
resource counts/size/total bounds and pending-control reserve.

Initial E0 remote, E1 removal-intermediate and E2 selected/manifest-final layouts
all contribute their exact v2/checksum/TREE bounds. The private index and full
index.lock each use the maximum, including deletion-heavy cases whose final tree
is much smaller. Generated final tree/subtree outputs and retained old baseline
storage are counted separately. NativeGit pins the admitted index/compression
profile. `core.worktree`, if present, must resolve exactly to the fixed native
root; arbitrary local worktree routing still refuses.

Only the actual active issuer-registered candidate Admission can qualify an
exact NativeGit adapter/root/executor/operation/owner. The registration ends with
the guard; copied/inactive or swapped Plan values refuse. Owner receipts occur
immediately after normal resource publication and outside its journal lock.
The existing one-snapshot bounded phase checks/frozen floor and the documented
pre-first-observation lock-history limitation remain. Lock-close failures retain
primary exceptions and addressed debt. See [native Save](native-save.md).


## Exact publication admission

The publication recipe accepts only the issuer-bound canonical PreparedPublication, actual immutable intent and exact prospective state bytes. It projects C/owner/pin/ref/reflog outputs without rebuilding candidate indexes or guessing outgoing pack demand. Own directory and byte/descriptor replacement receipts preserve the initial floor. A narrowly restricted pre-admitted recovery-state checkpoint can survive a changed local ref, but never changed owner/root/routes or foreign output and never authorize success. See [publication contract](native-publication.md).

Native publication future outcome recipes derive exact canonical extents from
fixed ASCII OID/hash slots before the single snapshot. Placeholder controls are
accounting inputs, excluded from ordinary output grants. Only owner-issued
compiled bytes from an exact qualified proof pass Admission; measured extent,
owner graph, source binding and every retained historical commitment remain exact.

## Explicit recovery qualification

Step 6.15 adds the separate `recovery_project` / `recovery_resume` seam for an
issuer-owned `native_recovery.Prepared`. It derives exact streamed recovery
copies, per-target-volume staged inodes and bounded control schemas before any
operation effects. Its immutable signed forecast retains original capacities,
reserves and inode floor across reopen. Targeted binding/current-state checks
remain the recovery owner's responsibility and run before each syscall. Generic
Plan issuance, one-shot Admission and existing namespace receipts are unchanged;
serialized generic Plans still grant nothing. See [the recovery contract](native-recovery.md).
