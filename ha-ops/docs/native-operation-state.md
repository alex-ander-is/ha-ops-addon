# Durable native operation state

Native-tree step 2 supplies an **inactive API** in `app/native_operations.py`.
Nothing imports it from the legacy server, dispatch, startup repair, or frontend.
It does not inventory live files, call Git or Supervisor, fetch remote refs,
perform migration, or schedule Apply. Later native steps must provide adapters
and explicitly wire the lifecycle. Existing Save/Apply still use `state.json`.

## Storage and schema

`NativeOperationStore(data_root, retention=32)` creates schema 2 at
`<data_root>/native-operations/state.json`, intended for persistent App `/data`.
The caller supplies an existing real directory outside the live tree. The
schema is independent of the App version. Missing manifests initialize an empty
store. Unsupported, unreadable and corrupt manifests block without replacing
their original bytes. Schema 1 remains readable under its original complete
history rules; reads and refresh never upgrade it. `upgrade(expected_revision)`
explicitly validates the entire original graph and every artifact before
atomically publishing schema 2 and collecting obsolete content.

The envelope retains the instance UUID, revision, global claim epoch, App
version, four current candidates, worker/receipt ledgers and baseline/active
policy. Schema 2 adds a monotonically increasing candidate generation, persisted
retention count H, compacted worker frontier/checkpoint, policy anchors, original
baseline provenance capsule and independent recovery pin. None of these
high-water marks resets on compaction, restart or update. No legacy server
preview or consent is imported.

Before the first Preview, an empty store has zero claim epoch and candidate
generation, no baseline provenance, and the default zero-frontier checkpoint.
Metadata-only compaction, lifecycle and recovery-pin commits may advance its
revision and App version but cannot create history. Every read and mutation
rejects uncovered high-water marks without rewriting the corrupt manifest.

Records contain exact direction, execution-policy version, scope revision and
allowlist/exclusion identity, local/base/remote ref names and OIDs, root device,
inode, mount and topology digests, and sorted path fingerprints. A fingerprint
includes file type, full mode, content digest and recoverable metadata digest;
missing paths have explicit absence markers. A migration adds the original
checkout identity, proposed commit and post-transition refs. A scope transaction
adds exact old/new policies. State carries hashes and minimal metadata, never
source file bytes. Complete reviewed diff bytes live in private SHA-256 artifacts.
Future producers must enforce secret-selection policy before calling this API;
there is no active artifact producer or generic debug export in this step.
Save/Apply review selections must also fall within the supplied scope allowlist,
matching complete path components. This check uses the reviewed policy and path
evidence only; it does not query inventory or expand exclusion rules.
Both review submission and persisted graph validation require a nonempty
Save/Apply selection for reviewed, consumed, completed and recovery-required
records. Empty maps remain valid for unapproved diagnostic records and for
migration/scope approvals, which do not select sync paths.

Directories are mode 0700 and files 0600. Traversal and file opens use
no-follow descriptors; manifests, locks and artifacts must be single-link
regular files. There is no fixed manifest, diff or cumulative-store byte ceiling.
A legitimate large fingerprint identity remains measured input. Real storage
errors still abort publication or leave an ambiguous acknowledgement after
replacement. The 1 GB advisory and projected peak admission belong to later
native-tree step 5; this API never evicts required recovery bytes to meet a quota.

`preview(bytes)` is a convenience; `preview_stream` accepts a binary reader or
iterator of byte chunks. Readers are asked for at most 64 KiB per read, and
supplied chunks are written/hashed in bounded pieces without copying a giant
chunk. The producer owns any memory it supplies. Size and SHA-256 are measured
through EOF. A private exclusive temporary is flushed/fsynced and its artifact
directory fsynced before publication can reference it. A preexisting digest
object must verify exactly, and its directory is fsynced before adoption even if
a previous producer left that name without completing directory durability.
Corrupt/missing referenced content is never repaired
from a producer. Producer exceptions and invalid chunks leave the old graph.

Every authoritative read streams all retained artifacts to EOF and checks both
hash and exact size. Review, claim and effect intent do this freshly, without a
size/mtime cache. `copy_diff(record_id, destination)` verifies the entire held
descriptor before emitting any bytes, then copies bounded chunks while holding
the cooperative writer lock. It handles short consumer writes. `read_diff`
returns complete bytes as a convenience and therefore needs memory proportional
to the diff. Descriptor identity does not freeze externally mutable bytes: the
private directory and all legitimate writers sharing the lock are the trust
boundary. Hostile in-place edits racing a verified copy are outside that boundary.
Read cost is bounded by retained object count plus complete referenced artifact
bytes, not by all historical Previews; large protected diffs still require a full
integrity read.

Optional history retains only the newest H terminal worker tuples. Superseded
unclaimed candidates have no history entitlement. Full pins are the union of
four current candidates, active worker record and independent recovery record,
so records/workers/receipts are bounded by H+6. Original baseline and latest
folded completed migration provenance are separate capsules whose digest
commitments do not retain obsolete diff files. Policy anchors are bounded by
H+8 and contain nonrecursive baseline or exact completed-publication sources.
Migration and publication capsules share this instance's allocated candidate
and worker namespace. Every available retained record/worker and other capsule
must agree exactly when a candidate UUID, generation, operation UUID or epoch
interval is shared, including original owner, marker, evidence, scope and binding.
This applies after a capsule's full source retires. The latest retained folded
completed migration must also match its capsule. A retained folded completion requires
its capsule. Available baseline and publication certificates also constrain
retired sources; obsolete full records/diffs and policy anchors are not required
to reconstruct intentionally discarded history.
Each policy has an exclusive start and inclusive end claim epoch: a completed
scope publisher belongs to its old policy, closes it at its terminal epoch and
starts its new policy strictly afterward.

Compaction folds only a validated terminal prefix into a checkpoint containing
the frontier, boundary policy, completed publication count and latest completed
migration capsule. An ancient pinned terminal tuple becomes an independent
archive below the advancing frontier; it does not keep intermediate history.
Archived tuples cannot claim or mutate work. The remaining worker intervals
must cover every epoch after the frontier contiguously; archives validate
individually with mutually exclusive intervals and exact saved policy anchors.
All retained candidates require established policy provenance. Forks, missing
intent, overlapping ranges, changed baseline, uncertified scope and expired or
future worker policy fail closed.

Repeated recovery takeovers also have bounded shape. The original acceptance,
effect intent, first recovery, latest owner and exact terminal evidence remain
visible; consecutive recovery transitions collapse into a typed first/last/count
range. Epochs still increase and the uncertainty fence remains. Short histories
remain explicit. Duplicate JSON keys, nonfinite numbers, unknown fields and
booleans as integers are rejected throughout both schemas.

`compact(expected_revision)` and successful mutations publish/fsync the new
manifest before collecting unreferenced digest artifacts and exact private
`.tmp-UUID` files. Collection uses no-follow descriptor checks, regular/0600/
single-link ownership and rechecks inode identity before unlink. Unknown names,
symlinks, FIFOs and hardlinks are preserved. Identical diffs share one artifact
until the last retained reference retires. Postpublication directory enumeration, unlink and directory fsync failures are
retryable debt exposed by `cleanup_errors`; they do not undo an accepted
mutation. A later mutation or explicit compact retries collection. Read and
refresh never sweep. `accounting()` reports artifact bytes, encoded manifest
bytes, temporary bytes and safely reclaimable bytes for later disk admission.

The committed checkpoint is a trusted local snapshot produced only by the fully
validating sole writer. Its canonical digest detects inconsistent edits; it
neither authenticates a self-consistent replacement of the entire private store
nor reconstructs deleted history. This is the existing cooperative-writer trust
boundary, not a new cryptographic historical proof.

One stable, separate `lock` inode is held with `flock` across read, graph
validation, revision comparison and atomic replacement. A process-local mutex
also serializes threads. Artifacts are flushed/fsynced and their directory
fsynced before a manifest can reference them. The manifest is written to a
private temporary file in the same directory, flushed/fsynced, replaced and its
parent fsynced. Errors after replacement are ambiguous acknowledgements:
reopen and replay the command, rather than assuming acceptance failed.

## Review, ownership and recovery

`preview` creates a new UUID and generation with empty decisions and no
approval. New approval bindings (binding version 2) cover exact identity,
artifact digest and length, decisions, instance UUID and generation. `review`
requires a complete fresh observation; `claim` rechecks it, consumes the approval
once and persists sole-worker acceptance before an adapter can act.

Every new claim must include keyword `instance_id` and `generation`. Its command
namespace is (instance, generation, command UUID), with request version 2. A
retained exact receipt replays its current accepted/terminal worker without CAS
mutation or effects. Changed payloads conflict. Retired, future, omitted and wrong
namespaces cannot create authority. Reusing a UUID with a different freshly
reviewed generation is an explicitly new request. Bounded storage intentionally
does not promise forever replay of discarded commands.

Upgrade preserves original schema-1 approvals and worker bindings byte for byte
(binding version 1): SHA256 of canonical `{identity, artifact: digest_string,
decisions}`. Measured artifact size is checked independently. Existing receipts
keep request version 1 and their original `{record_id, binding}` digest. The
original tokenless request may replay only a retained v1 receipt and never accept
new work. Before upgrade, any supplied v2 namespace conflicts; explicit null
arguments are supplied namespace fields, not an omitted tokenless request. An unconsumed migrated reviewed candidate requires a new v2 generation
request while retaining its original v1 approval. V2 receipts cannot enter legacy
lookup; failed namespaced traffic cannot fall back to it. Replay never restores
consent or launches a worker.

`recovery_pin(expected_revision, expected_slot, record_id, identity_digest)`
protects an exact retained candidate independently of the active worker. Replace
or close it only with the exact prior slot and CAS; omit the new record/identity
to close. Refresh, lifecycle, update and compaction preserve it. This slot protects
content for future recovery; native-tree step 12 still owns actual recovery
journals, backups, adapters and verified rollback.

The worker has an operation UUID, owner process UUID, monotonically increasing
epoch and independent external effect marker. Every worker mutation requires
the current revision, operation UUID, owner and epoch. Opening another store
or reading it never takes over a worker. `checkpoint(effect_started)` requires
another fresh complete observation immediately before a future adapter effect.
The adapter must make that durable checkpoint first, then perform its narrowly
scoped effect. This module does not implement the adapter or its race checks.

The first durable Preview in any domain atomically anchors its supplied scope
as the immutable baseline and active policy, with that candidate's UUID and
identity retained in an independent provenance capsule. Only a truly empty store can have no policy. Cancellation,
superseding a candidate, refresh, restart and version update retain this anchor.
All later Previews must use the active policy; changing it requires a completed
scope publication. On every read, including stores with no scope transactions,
validation derives the active policy from the certified checkpoint boundary
and ordered retained suffix of completed publications in exclusive epoch order. Policy
revision numbers do not establish chronology. Every worker must have claimed
the scope confirmed before its operation; sync under a future or expired policy
is corruption. Every retained candidate must reference an established policy.
Each old policy must match the preceding confirmed policy; forks, skipped publication history and an active policy with
no corresponding completion are corruption. A typed policy alone cannot grant
new-scope authority.

`completed` requires exact before/after evidence digests and the worker's effect
marker; a generic success flag cannot complete it. These values are declared
in the reviewed identity. Future adapters are responsible for obtaining fresh
trusted evidence of the real effect. Deterministic input tests establish this
state gate, **not** remote/live reconciliation. An accepted operation can fail
without effect. Once an effect may have started, failure/uncertainty must retain
`recovery_required`; it cannot release the fence by reporting an ordinary failure.

| Event | State behavior |
| --- | --- |
| Page refresh or new tab | `refresh` returns diagnostic candidate/artifact/checkpoint and worker status, with an empty draft and no candidate decisions/approval. It does not mutate or launch work. |
| Explicit restart takeover | `lifecycle` fences the previous owner with a higher epoch. Accepted-before-effect work becomes invalidated and needs a new Preview/review. Effect-started work becomes `recovery_required` and keeps its consumed review and checkpoint evidence. |
| App update | The same explicit lifecycle validates schema and rechecks structured identities/execution policy. Compatible unchanged migration/scope approval and artifacts survive. No schema reset or auto-resume occurs. |
| Missing, unavailable or changed observation | Approval and decisions lose authority; diagnostic diff and checkpoint history remain. A new Preview UUID and explicit review are required. |
| Recovery | Only exact matching external outcome evidence can complete uncertain work. It stays fenced until then; no automatic retry, ref update or push occurs. |
| Scope cancellation | The old active policy remains; no live file is deleted. |
| Confirmed scope completion | Only matching completed publication evidence advances the active policy to the proposed revision. Pending/interrupted scopes retain the old policy. |

The observation seam accepts `{available: true, identity: <complete identity>}`.
No observation, false availability, partial fields or mismatched inputs deny
new authority. It has no network behavior. Later adapters must freshly observe
remote reachability/OIDs, filesystem fingerprints and mount/policy identity;
they must recheck each target immediately before replacing it and use expected
old OIDs for Git updates. This module makes no claim of atomicity between Git
refs and filesystem writes. Metadata restoration, per-path journals, migration
phases, trusted publication reconciliation and native UI are later steps.

## Verification and evidence

`tests/test_native_operations.py` covers strict schema/graph corruption,
private/no-follow storage, artifact corruption, every persistence boundary,
real separate-process crashes, competing claims/CAS, killed lock holders,
owner fencing, replay, source invalidation, refresh/update and legacy isolation.
The local Linux wrapper runs these tests without network or live mounts.
Its optional `--recreate` probe runs twice in distinct containers against the
same disposable output bind, comparing manifest/artifact bytes and checking
a higher ownership epoch, retained old scope, independent recovery pin, advanced
frontier/generation and exact streamed 17 MiB content across replacement.

Sources used: [Python 3.12 flock](https://docs.python.org/3.12/library/fcntl.html),
[fsync, replace and descriptor-relative APIs](https://docs.python.org/3.12/library/os.html),
[strict JSON considerations](https://docs.python.org/3.12/library/json.html#standard-compliance-and-interoperability),
[HA persistent App storage](https://github.com/home-assistant/developers.home-assistant/blob/master/docs/apps/configuration.md),
[Git expected-old-OID updates](https://git-scm.com/docs/git-update-ref.html),
[Git ref name validation](https://git-scm.com/docs/git-check-ref-format).
The architecture also compared [Terraform saved plans](https://developer.hashicorp.com/terraform/cli/commands/plan)
as an example of binding later execution to an explicitly reviewed candidate;
no Terraform code or execution assumptions were adopted.
