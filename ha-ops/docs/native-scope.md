# Dormant native folder scope API

`native_scope.NativeScope` is an inactive orchestration API for the future native
folder tree. Production startup, dispatch and UI do not import it. No live Apply,
installation of policy files, fetch on refresh, or automatic publication exists.

Construct it with an exact `NativeSave` and its canonical `NativeOperationStore`.
Construction has no I/O. `diagnose()` validates existing state without initializing
missing state. A real established native baseline is required before `build`.
The caller supplies fresh, authoritative source observations through NativePreview.

`build(proposed, approved_parents=..., expected_revision=...)` begins or revises one
pending transaction. Nested byte paths normalize once; empty scope is valid. Only
newly included live leaves replace remote blobs. Excluded tracked leaves are
removed from the candidate index while live files remain present. Unchanged
remote content and shared-parent attributes survive. Required new directory
attributes need explicit parent approval. Re-inclusion captures current live
bytes; history never restores excluded content to the live tree.

The issued `ScopeReview` identifies old/new policies, complete observed membership,
parent approval and exact generated ignore bytes. The `.gitignore` contains its
own re-inclusion, the fixed metadata exception and a canonical scope comment.
`reviewed_diff()` returns a metadata-only `native-scope-review-v2` canonical
envelope: typed review/candidate, final and baseline content references, removed
before metadata/references, excluded unsaved states and historical context. Each
`ContentRef` records raw SHA-256, exact size, Git blob identity/format and binary
flag. Payloads live independently as immutable private `<sha256>.blob` files.

`copy_review_content(section, path, destination, commit_oid=...)` streams one
complete verified file to a sink. Sections are `final`, `baseline`, `removed`,
`excluded`, and `history`; historical selection requires the exact commit OID.
Absent entries remain explicit and have no content to copy. Selection resolves
only entries of the current canonical pending review. Raw/Git hashes, exact size,
binary flag, no-follow regular descriptor, private mode, single link, and held
route identity are verified before emission and after consumption.

Build measures sources through fixed-size digest sinks before any output effect.
The one admitted plan includes every payload, temporary/final overlap, exact
metadata/state atomic overlap, entries/inodes and candidate Git resources. The
issued immutable prepared recipe binds each producer/reference to the operation
and owner; disk projection independently remeasures it. After admission, a held
canonical scope lock excludes compaction until durable payload installation,
streamed candidate materialization and canonical state CAS finish. Output receipts
verify binary files independently of bounded JSON control readers. Content memory
is fixed by chunk size plus metadata proportional to path count, without a file
size ceiling. Durable review survives Git pruning independently of Git objects.

Only the canonical pending scope owns the complete binary closure, including
history and excluded content. Refresh, restart, update, compaction, publication
and recovery retain it. Replacement first installs the new closure and then
switches authority atomically. Completed publication cleanup or explicit cancel
releases binary ownership; retained history and active-rule provenance keep only
metadata. Subsequent compaction removes unowned payloads and records unlink errors
as cleanup debt. Legacy `native-scope-v1` state can be diagnosed and cancelled
with streaming artifact verification; reopen refuses with an explicit migration
requirement instead of decoding aggregate content.

`confirm(candidate, review, publication, **confirmation)` requires the exact current
issued pair and the same NativeSave/store peers. NativePublication independently
reconstructs the complete scope candidate and runs the existing expected-old,
authenticated transport, graph-proof, local-CAS and owned-cleanup protocol. The
canonical publication record uses the scope domain. Active scope and rule bytes
advance only in the final publication cleanup transition, after positive proof,
local CAS and required resource retirement. A completed worker with cleanup still
pending leaves the old policy active and Apply blocked. Generic store commands
cannot approve, claim, checkpoint or cancel the issued scope record.

`last_save_offer()` exposes excluded unsaved path/state/reference rows. Declining requires no action.
`review_last_save()` creates an ordinary, separate old-scope Save review.
`confirm_last_save()` is the separate explicit publication confirmation. It retains
the scope transaction and invalidates its old issuer grant. A fresh source and an
explicit scope build/review are mandatory afterward. Ordinary Save candidates can
carry an exact `ScopePolicy` through `build(..., active_policy=...)`, preserving
canonical revisions after the first scope change.

`lifecycle('refresh'|'restart'|'update')` clears ephemeral grants and selections,
retaining canonical transaction and full reviewed bytes. It performs no fetch,
publication or rebuild. `reopen_review(expected_revision=...)` explicitly verifies
source, complete current membership/content/metadata, binding and durable blobs,
then admits re-materialization and issues a new bounded grant for the same tree
and diff. Source or live changes require an explicit new build. Publication
recovery uses the same retained intent/commit through `recover_publication` and a
fresh one-attempt approval; it does not rebuild another commit.

`cancel(expected_revision=...)` admits a canonical state-only transition, retires
the pending linkage and preserves old active policy and all live bytes. An active
or unresolved publication must reconcile first. `fresh_apply_preview()` uses an
independent Preview session and the current old policy while any transaction is
pending. It observes fresh sources, supersedes its prior Apply result/generation,
and returns empty drafts plus authoritative transaction/Apply-blocked diagnostics.
It does not consume the scope grant or replace publication recovery state. The
store also rejects old Apply review/claim paths while a scope is pending.

## Verification boundaries

`test_native_scope.py` contains portable no-I/O/codec/pre-effect recipe guards and
real Linux candidate, lifecycle/GC, cancellation, publication cleanup and final
Save scenarios. Portable forecast interception does not prove Linux admission.
Use the repository's existing local virtualization wrapper and cached image;
never use the live HA host as a runner. The normal root-capable Linux profile is
needed for arbitrary UID/GID and authenticated-network fixtures. A supported
nonroot ext4 run can prove own-UID scope cases only, and must also execute the
complete required ext4 qualification matrix. Host skips are not operational proof.

An already tracked denied path or an invalid/mismatched baseline manifest blocks
scope construction without live, index, ref or canonical-state writes. This API
does not weaken the secret policy to remove unsafe historical entries. Their
disposition requires a separately reviewed repository repair before native scope
work can resume. This follows the product's requirement to block publication
until disallowed tracked entries have a resolved disposition.

Re-inclusion review also carries typed last-managed comparison context. Explicit
build walks exact commit parents rooted at the current authoritative remote,
retaining the nearest managed revision's complete content reference and supported leaf and
required-parent metadata. Equal-distance merge ancestors remain separate contexts;
paths absent from the entire observed ancestry carry explicit no-history entries.
Unavailable historical metadata blocks rather than inventing attributes. The
context is durably serialized with the review and independently verified before
publication/reopen. It never supplies the proposed tree or writes live files.

Pending scope authority has its own canonical owner and revocation epoch.
Canonical lifecycle takeover advances it, expiring retained scope and optional
final-Save receipts even when a caller supplies the latest state revision.
Explicit admitted reopen renews that authority while preserving the same candidate
and immutable diff; direct NativePublication calls enforce the same fence.

The historical comparison universe covers all historical managed leaves in the
newly included folder region, including files deleted while excluded and an
entire folder currently absent. Each scoped context explicitly records current
presence; current absence remains absent in both proposed Git content and live
files. Build, publication and reopen derive this complete universe from the same
old/new folder membership rather than a submitted subset of present selections.
Issuer replacement publishes one complete receipt with its immutable canonical
binding. Failed validation, Busy acquisition, edit or reopen either retains the
original bound receipt or revokes it completely; clearing a nullable association
cannot turn an expired scoped grant into an ordinary unbound grant.
