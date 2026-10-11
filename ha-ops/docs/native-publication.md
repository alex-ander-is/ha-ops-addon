# Dormant native reviewed publication

`NativePublication` is an explicit dormant caller of `NativeSave`, the canonical
`NativeOperationStore`, `NativeDisk` and local-only `NativeGit`. Production Save,
Apply, startup and UI do not import it. It writes no live configuration or
canonical index. The supported outgoing transport is an explicitly supplied
local bare repository; SSH/HTTPS/authentication and implicit fetch are outside
this qualification.

## Authority and exact bytes

Only the latest exact Candidate and its original ready Review issued by the
same Save/Preview/Git/Disk session can start. A nonblocking publication session
holds the selected-source fence through acceptance and the final pre-send
check, without applying the obsolete original-remote fence after publication.
Copies, restored diagnostic bytes, pins and lifecycle ownership are not grants.
Detached typed snapshots bind every Candidate, original Review and nested binding/policy field at issuance. Same-object mutation cannot preserve authority by recomputing a valid subset or digest. Consumption clears the live receipt before the first owned allocation.

Full raw-hash verification reconstructs the remote-plus-selected tree and
manifest, including unselected dependencies. The original source must be a
commit. Explicit author, committer, timestamps, timezone and opaque message
bytes freeze one raw commit C: exactly tree T and exactly one original parent R.
Signature edges that Git strips, negative-zero timezone and any installed Git `var` formatting mismatch refuse before owner effects. A read-only fixed-environment formatter verifies both identities before acceptance; accepted interior punctuation, Unicode, plus-address email and nonzero zones retain exact Git reflog bytes. The canonical immutable intent contains these bytes, exact endpoint/destination,
original local expected-old value and real operation/record/owner namespace.
Lossless `native-bytes-v1` tokens encode each byte component as lowercase hex;
`nb1/...` is relative, `/nb1/...` absolute and `/nb1` represents `/`. They have
no inherited display-text limit. Existing label/ref and legacy path bounds remain.

## Effects and reconciliation

A read-only owner preparation validates actual canonical artifact and all
prospective state bytes before one NativeDisk snapshot/project/guard. The
publication recipe accounts for existing T, exact loose C, owner artifact/state,
locks, atomic overlap, private tree/commit pins, direct local ref and exact
existing reflog append. Sender pack-objects streams through stdout; there is no
invented local pack/index/spool demand. Receiver storage is external to admission.
Planned parent creation uses exclusive mkdir and descriptor receipts.

Future observed OIDs and SHA-256 commitments have strict ASCII widths. Owner preparation derives exact canonical byte extents for fixed outcome recipes before the snapshot; sentinel values are accounting inputs only. The compiler consumes an exact issuer-qualified proof and fills only tip/graph/transcript slots, checks the fixed operation/owner/epoch/attempt/request binding, validates the complete resulting owner graph and exact byte extent, and registers immutable actual bytes. Admission accepts only those issued compiled bytes. Direct shape-valid compiler calls and accounting templates provide no output authority. Later lifecycle recipes require the exact retained issued confirmation.

The sole owner durably accepts intent, marks preparation started, hardens the
verified objects, creates expected-absent T/C pins and writes exact C. Before
push it durably marks `publication_attempt=possible`. Git CLI sends only
`C:destination` with `--force-with-lease=destination:R`. Sender hooks, helpers,
negotiation, signing, extra push options, tag/submodule updates and maintenance
are disabled; receiver policy remains its own. Config/routing/alternate/shallow
changes invalidate the qualified profile. The fixed receive-pack invocation
removes sender command-config propagation rather than disabling receiver hooks.

Fresh exact advertisement plus independently hashed commit-parent links prove
C reachable from the current remote tip, including a descendant D. Complete
negative ancestry and an unchanged re-advertisement are required for refusal.
Unknown graph, timeout, partial/mixed status and generic `!` leave recovery.
Only a producer-issued no-send or exact single-ref no-update outcome in the
original attempt can terminate after verified owned cleanup. A later absence
cannot erase a prior possible/ambiguous attempt.

The owner persists the exact observed tip, graph commitment, request commitment and available push-transcript commitment before one direct expected-old local CAS to C. `publication_outcome` preserves the first actual positive proof on the worker and historical checkpoints. A native-only same-phase revision permits only the first `None -> published` proof; later recovery never rewrites that evidence. `publication_confirmation` separately records the current proof before CAS, including source owner/epoch. Fresh reconciliation uses strict `readback` mode and a null transcript, retaining the original historical attempt rather than inventing a new push transcript. Current C is
already advanced; recovery re-fsyncs the exact ref or packed-ref descriptor, existing admitted reflog descriptors and their parents before completion, without another CAS or append. GC may expire or rewrite historical reflog entries; recovery hardens the unchanged current captured log layout rather than recreating an expired event. Original CAS provenance remains in the immutable intent and first owner proof. Continuing EIO retains recovery; any unrelated local value is preserved. Existing admitted ref/HEAD reflogs are explicitly fsynced with their verified descriptors and parents after CAS. New absent reflogs are disabled for this publication command, so they cannot allocate unadmitted routes. A hardening failure retains recovery even when the ref already advanced. Completion retains an
independent canonical recovery pin until owned Git pin cleanup is acknowledged. A qualified actual refusal, including observed tip/graph/transcript/request commitments, also persists before either Git pin can be retired.
Native terminal cleanup has strict durable progress 0 -> 1 (commit retirement authorized) -> 2 (commit resources acknowledged) -> 3 (tree retirement authorized) -> 4 (tree resources acknowledged). Odd owner transitions consume an issuer-qualified unchanged pin observation. Even transitions require a separate issuer-issued ACK after actual pin and both own locks are absent, current packed bytes/descriptors are fenced, both relevant parents are hardened, Admission accepts actual resource receipts and every held descriptor closes successfully. Every capability binds the exact prepared owner state and Admission. Reopen repeats hardening and actual receipts for authorized previously acquired own resources; neither authorization nor observed absence supplies an ACK. Unexplained absence, foreign OIDs and symbolic occupants refuse. Final slot release follows progress 4. Pending terminal cleanup blocks a new publication and remains pinned through retention/lifecycle. The native worker's fixed `publication_cleanup_owner={owner,epoch}` starts at original ownership. Explicit terminal lifecycle takeover updates only this authority and the global epoch; worker owner/epoch, first outcome and every historical checkpoint remain unchanged. The retained recovery slot, exact current lifecycle epoch and unchanged sole-owner state bind explicit cleanup only. It cannot authorize push, local CAS or reconciliation advancement, and lifecycle performs no pin cleanup. Active workers keep established takeover semantics. Reopened terminal cleanup requires no already-retired C/T object; it admits only owner settlement and remaining own-pin removal. Pin deletion holds the exact ref lock, verifies the original descriptor/OID and
fsyncs the existing parent; it does not prune shared directories. After GC,
the recipe captures actual packed-ref bytes and only the finite exact own-pin
removal variants. A held exclusive packed lock protects atomic replacement;
independent byte/descriptor receipts update the cached profile. Every foreign
packed row and peeled row remains byte-for-byte unchanged. Primary errors
retain operation/ref/resource addresses and secondary cleanup failures.

## Restart and explicit recovery

`diagnose` reads retained intent/current owner/epoch, pins and local state with
no execution authority. Refresh, reopen and lifecycle/version handling never
push or resume. `approve_recovery` creates a fresh issuer-bound one-attempt
capability for `reconcile`, same-C `push`, terminal `cleanup`, or explicit
`abandon` when the durable marker proves no push could have begun. Before any Git/transport/cleanup effect, every explicit recovery command durably stores a native-only request-version-3 receipt bound to operation/generation, intent/request digest, owner/epoch and pending effect. Retained command IDs refuse replay across reopen, revision changes and lifecycle takeover. The fixed profile allows 128 transport `push` receipts and a separate 16-receipt settlement reserve for `reconcile`/`cleanup`/`abandon`; transport-cap exhaustion therefore permits ordinary settlement. Each bucket's exhaustion fails closed before effects with capacity diagnostics; settlement exhaustion remains a bounded availability limit, not unlimited recoverability. Compaction removes receipts only with a retired operation; that missing operation can no longer be recovered. Exact added receipt/state/checkpoint bytes are prepared and projected before the sole snapshot. Every actual
attempt gets a new snapshot. Stale/copied/mutated/replayed grants refuse.

The native profile extends schema-2 workers and checkpoints with the strict
monotone `not-sent -> possible` attempt marker. Old generic readers reject this
extra grammar, including required outcome/confirmation/cleanup fields and native recovery receipts; legacy schema-2 workers remain unchanged. Outcome source ownership survives lifecycle epoch changes, bounded summaries and canonical capsules; current completion still requires a fresh proof from the current owner. No native publication
records existed at the baseline, so no fabricated old-native upgrade is claimed.
Chronology survives lifecycle takeover, bounded recovery ranges and capsules.
A possible historical attempt cannot be abandoned using today's negative graph.
An interrupted proved no-send abandonment remains explicitly settleable after
its durable refusal or any pin-retirement checkpoint, including reopen and
cleanup-owner lifecycle takeover. The unchanged failed/not-sent/known-not-sent
proof and original matching abandonment command receipt authorize settlement of
never-acquired absent pins. Neither absence nor an empty resource token grants
lock ownership: unknown creation-before-record and foreign locks remain protected
cleanup debt. This terminal continuation never pushes or advances a local ref.
Every independent held descriptor and transport pipe receives one close attempt
even if an earlier close fails. The exact first exception survives, subsequent
failures carry resource addresses, and no failed close yields a retirement ACK.
Missing/changed acknowledged resources and cleanup failures remain addressed
recovery debt; no foreign occupant is adopted or removed. A failure checkpoint
may persist only pre-admitted recovery bytes after a Git-ref conflict, with the
same verified owner/root/routes/reserve. This grants no Git or success authority.
Save completion leaves the active scope unchanged.

## Qualification and limits

Focused tests use real Git 2.39.5 in the existing local Linux runner, disposable
bare receivers and normal tmpfs. Executable receiver hooks live in individually
owned output directories because the runner's `/tmp` is noexec. Actual transport
SHA-1/SHA-256, receiver rejection/post-receive failure, lost/mixed acknowledgments,
remote races/fresh review, descendant readback, CAS recovery, long Unix byte
paths, same-object receipt tampering and reopen chronology are exercised.
Real child SIGKILL/GC/reopen tests exercise all seven persisted boundaries with SHA-1 and SHA-256 GC cases. Separate publication observer telemetry checks actual owner temporary/retained controls,
loose commits and held ref locks against the publication bytes/inode forecast
on tmpfs and the existing opt-in ext4 fixture. These checkpoint observations
are not a power-loss simulation or protection against external allocations.
Host skips are not Linux proof.
Cooperative admission cannot prevent external disk consumption or kernel I/O
failure, and descriptor caches cannot prove substitutions before observation.

Protocol evidence: pinned [Git 2.39.5 transport.c](https://raw.githubusercontent.com/git/git/v2.39.5/transport.c),
[connect.c](https://raw.githubusercontent.com/git/git/v2.39.5/connect.c) and
[send-pack.c](https://raw.githubusercontent.com/git/git/v2.39.5/send-pack.c).

Signature serialization evidence: pinned [Git 2.39.5 ident.c](https://raw.githubusercontent.com/git/git/v2.39.5/ident.c), plus the actual installed fixed-environment `git var GIT_COMMITTER_IDENT` qualification.

GC reflog evidence: pinned [Git 2.39.5 files-backend.c](https://raw.githubusercontent.com/git/git/v2.39.5/refs/files-backend.c), `expire_reflog_ent`/`files_reflog_expire`; actual GC/reopen local-CAS regressions qualify both object formats.


R2 canonical and primitive closure
-------------------------------

Generic Preview/create, Review, claim, checkpoint, cancel and protection APIs
refuse native publication authority. Generic protection cannot release or replace
its recovery slot; generic completion cannot promote a stale historical proof.
The canonical validator independently requires the exact recovery protection for
every pending terminal native cleanup and a current-owner confirmation at success.
Read/refresh are diagnostic; compact retains protected operations, original proof
and all recovery IDs; lifecycle transfers only the established active authority or
terminal cleanup authority. Legacy generic controls keep their existing grammar
and behavior. A new Preview/claim cannot bypass outstanding native cleanup debt.

Each native worker has four fixed `publication_resources` slots: commit,
commit-packed, tree and tree-packed. Each is exactly 176 lowercase hex characters,
initially zero (unacquired), or eleven unsigned 64-bit real descriptor fields:
file device/inode/mode/size/link count/mtime_ns/ctime_ns/blocks followed by parent
device/inode/mode. Nonregular/multilink/unsafe modes and stat overflow refuse.
The issuer captures these fields from the actual exclusive lock descriptor,
verifies its exact route and bytes against Admission, and persists its identity
before pin unlink or packed replacement. The compiler consumes a sealed unchanged
one-shot producer capability; shape-correct caller tokens grant nothing. Existing
locks are adopted only with this persisted exact descriptor/parent identity and
bytes. Name and matching OID payload alone never establish ownership.

The four resource strings add exactly 788 canonical bytes per native worker,
including their field name, keys and comma. They are present in every
prospective worker state before the sole snapshot. The bounded cleanup recipe adds
four optional resource transitions, with explicit revision positions reserved for
main and packed locks before the next progress step. The canonical revisions
strictly advance even when optional packed work is skipped. Templates are only
accounting inputs; compilation changes these four exact slots only for the current
operation and independently validates the resulting graph and unchanged measured
byte extent. Earlier resources, original outcomes, checkpoints and retained
command IDs remain unchanged. The forecast includes each actual resource state
and atomic control overlap; no numeric caller ceiling or unbounded artifact is
introduced. Old readers reject these native-only worker fields; no native
publication records existed at the baseline, so no old-native upgrade is claimed.

A crash after lock creation but before its ownership record can leave an unknown
lock. This remains addressed cleanup debt and fails closed across reopen and
lifecycle, even if its bytes match the expected OID. This boundary deliberately
does not claim automatic recoverability. Persistent unlink, parent fsync, packed
replace/receipt or close failures retain the canonical recovery protection; cleanup
cannot report completion merely because the pin disappeared. Verified recorded
own locks can be settled by a fresh explicit cleanup command under the current
lifecycle owner. Foreign or changed resources remain untouched.
