# Dormant native Apply candidate

`app/native_apply.py` is an explicit candidate API. Production startup, ingress,
the broker and the ordinary direct listener do not register its worker or enable
its actions. Step 24 remains the separate activation boundary.

`NativeApply.prepare` consumes a current issued Apply Review, advertises the
actual qualified remote first and freezes whole selected paths, numeric metadata
and reviewed parents. File content is streamed into private, bounded, individually
issued source files. A copied Review, a serialized descriptor or authentication
alone supplies no operation authority. Save uses its existing distinct context.

One original issued NativeDisk Snapshot supplies the available-byte/inode floor.
Apply-specific demand extensions qualify spools, merge overlap and participant
controls against that same floor before allocation. The private admission recipe
is published before source allocation and records independent source receipts.
A per-operation MAC key authenticates every recipe, late source receipt and
cleanup intent/ACK update. Before claim, the immutable operation artifact binds
the original namespace identity, original receipts and key; no extra key file or
capacity sample is introduced. The private key is never part of status or error
projections. Reopen verifies this independent artifact anchor and the complete
signed recipe schema before issuing cleanup source handles. Control loss can
retire an empty namespace only when its original artifact-pinned inode matches.
Recovery installation consumes this admission rather than sampling capacity again.
Kernel IO failures remain failures even after successful admission.

Unclaimed preparations have a cleanup-only capsule in the existing private
RecoveryStore. The original disk floor qualifies key/lock infrastructure and two
capsule control copies before allocation. A signed self-pinned capsule precedes
namespace creation; creation and inode acknowledgment share the same interprocess
fence. Exact namespace authority is durable before the first source byte. A
process death before namespace creation permits explicit capsule retirement;
creation without an inode acknowledgment remains visible addressed debt and is
never inferred as ownership.

`NativeApply.cleanup_drafts()` discovers unclaimed capsules without writing or
reconstructing a Review. `cleanup_draft(operation)` explicitly removes exact owned
resources and retires their capsule; repeated completed cleanup is idempotent.
Claimed commands require their reviewed recovery/abandonment path. Refresh clears
session selections and consent; a fresh process has no preparation or bypass
consent. Restart/update lifecycle preserves no-dispatch provenance for explicit
cleanup, including null pins, without authorizing Apply or replaying services.
Later Apply always requires a fresh Review and explicit consent. A shared stable
Binding root directory-inode lease serializes explicit draft cleanup with source
recapture, immutable intent publication and durable claim/pin. Claim validates its
original capsule and sources inside that lease before allocating more sources.
Cleanup checks claim ownership inside the same lease, so neither ordering can
turn accepted command resources into an unclaimed draft. Fresh UUID namespace
leaves are exclusively created; existing ancestors may be reused after checks,
but an existing leaf is preserved as acquisition debt.

The explicit worker classifies backup inventory read-only. Missing/stale evidence
permits one matching explicit bypass only after another successful inventory read;
unavailable or invalid inventory never does. This module has no backup-create path.

Before any stop, the worker claims and pins its durable operation, freezes the
ServiceStore inventory and installs the independent recovery point. It waits for
an authenticated browser challenge response from the actual optional DirectServer
peer. NativeStatus must be bound to the issued preparation and its exact current
operation, service and recovery journals. The production default remains
unavailable. Same-origin CSRF, listener identity, expiry and one-use receipt checks
are independent of Apply consent. Listener closure revokes readiness; each stop
checks it again. A reconnect reads durable command results without replaying work.

Every forward mutation remains a RecoveryStore journalled effect. Exact Git
authority is checked after stop and immediately before dispatch. Compatible text
changes use real sanitized Git merge-file output. An untouched later leaf can
publish a successor immutable recovery generation under the same ownership and
original disk floor. Required parent UID/GID and full 07777 modes are rechecked
before recovery compilation under the same three-way rule as selected leaves;
conflicting independent edits refuse before installation or stops. Parent route
inodes remain qualified independently of compatible metadata changes. It preserves every earlier original preimage and effect
receipt, including deletion tombstones. A self-contained sealed successor owns
independent payloads, stages and inherited effect anchors. After durable pointer
publication, predecessor generations and unused stages retire; exact inherited
tombstones transfer to the active generation. A retirement failure retains at
most the active and pending generation and blocks another amendment. Demand
credit requires independently verified removal under the bound root; inherited
live replacement inodes remain charged. Reopen only discovers these journals.

Validation or readiness failure retains the point. If acknowledged starts already
occurred, ServiceStore persists one finite compensating cycle, quiesces those exact
services and preserves the original inventory and prior receipts before rollback.
Compensation and restoration require the current operation and owner; a previous
restored lifecycle is retained as unrelated history and receives no commands.
Originally stopped services and HA Ops remain excluded. Unknown dispatch cannot
become ownership from current process state. Successful health produces a durable
terminal result; residual live/Git differences are fresh WARNING rows with empty
selections. Success cleanup is separate from rollback and does not undo a healthy
operation merely because cleanup failed.

Forward phases share one remaining monotonic deadline, including validation
return and terminal publication. Failure rescue has an explicit separate
`recovery_seconds` allowance (30 seconds by default), shared across its service
compensation/restoration calls. An expired forward operation cannot become
SUCCESS by reacquiring a phase timeout.

A healthy terminal checkpoint initially stores cleanup-pending ERROR. Guarded
retirement, source cleanup and pin release publish final SUCCESS/WARNING. Failure
persists cleanup debt for both the initial caller and reconnect. Reviewed close
records a signed transaction and resolves its matching operation fence;
`resolve_closed_recovery` explicitly resumes an interrupted recorded close. New
independent Apply replaces only an exact restored service journal.

Source cleanup persists an unlink intent and removal acknowledgment for each
exact command-owned resource. Retries and fresh-process explicit close reopen
only the operation UUID namespace and its original stat/content receipts; an old
close replay resolves its recorded operation rather than a later draft. Completed
healthy cleanup debt can be reviewed with either a retained or absent pointer.
An accepted worker has not dispatched service or file effects: effect-started is
published after both participants install and before the first stop. A fresh
explicit no-effect abandonment can close an accepted worker without participants,
or a durable synchronous pre-dispatch failure, through the existing operation
CAS and exact resource cleanup. Both absent and exactly owned recovery pins are
valid pre-dispatch prefixes; a foreign pin refuses before cleanup. An own prepared
or restored zero-command service inventory can be retired without dispatch, while
an exact previous restored lifecycle remains untouched. Unclosed no-effect debt
retains its artifact and blocks a new Apply until explicit settlement. It does not construct a recovery point.

## Qualification evidence and limits

The isolated Linux integration suite composes actual Git advertisement,
authenticated HTTP, recovery effects, process-service fixtures, compatible late
merges, repeated successor rollback, per-resource cleanup retry and explicit
close interruption/reopen. The rendered Linux/browser relay is separate evidence;
these fixtures do not qualify a production Supervisor transport or activate it.

`tests/test_native_apply_ext4.py` is opt-in under the isolated allocation ext4
profile. It measures the composed worker's physical peak and inode demand against
the persisted original-floor forecast, including large compatible merge and
startup compensation. It also exercises actual kernel ENOSPC, injected EIO at the
generation, stage, pre-intent publication, durable-intent and effect/ACK
boundaries, and real process exits across participant boundaries.
Observational reopen assertions are distinguished from explicit reconciliation,
rollback, reviewed close and no-effect abandonment.

These tests qualify isolated native primitive composition and distinguish
observational reopen from explicitly requested cleanup or recovery. They do not
qualify a production Supervisor transport. Step 24 remains the separate production
activation boundary.

Apply resource mutations use one nonblocking lease on the existing qualified
Binding root directory, acquired before bootstrap allocation. A registered
capability identifies the operation, participants, purpose, PID, thread and
context lifetime. Helpers receive it explicitly; copies, inherited or expired
capabilities cannot authorize writes. Status and authenticated Direct HTTP
reads do not acquire this lease or the Preview session lock. The lock order is
root lease → Preview → RecoveryStore → bounded read-only Git/transport and
ApplyAdmission operations. Recovery callbacks do not acquire operation or
service mutation locks or reacquire the root/recovery lease.

Every draft cleanup entrance first rechecks durable worker ownership under that
lease. A signed `retiring` capsule is published before source deletion and
survives admission-control and namespace deletion. Explicit retry uses its exact
original receipts; a claimed worker requires proven healthy terminal cleanup or
fresh reviewed close. Replacement preparation verifies the actual remote first;
failed remote access preserves the previous draft. Explicit reset and close
remain local actions.

The recovery compiler accepts an independently issued capture and target pair.
The capture covers exact leaf bytes, full numeric metadata, absence, inode and
ancestor closure. Content and metadata decisions derive from those frozen
values. The complete compiled before map and derived after map must match that
issuance, and the exact manifest digest binds operation admission. A change
between capture, merge and compilation refuses before claim; install and effect
CAS fences protect later drift. Successors retain the same complete closure
checks and original affected receipts. Controls added for late result/residual
paths are qualified before publication against the original disk floor.
