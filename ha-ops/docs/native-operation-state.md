# Durable native operation state

Native-tree step 2 supplies an **inactive API** in `app/native_operations.py`.
Nothing imports it from the legacy server, dispatch, startup repair, or frontend.
It does not inventory live files, call Git or Supervisor, fetch remote refs,
perform migration, or schedule Apply. Later native steps must provide adapters
and explicitly wire the lifecycle. Existing Save/Apply still use `state.json`.

## Storage and schema

`NativeOperationStore(data_root)` stores schema 1 at
`<data_root>/native-operations/state.json`, intended for persistent App `/data`.
The caller must provide an existing real directory, outside the live tree.
The envelope has a revision, instance UUID, monotonic claim epoch, timestamp,
App version, immutable candidate records, current pointers for migration,
scope, Save and Apply, a worker ledger, replay ledger, immutable baseline scope,
its retained source-record UUID and active scope.
Schema version is independent of the App version. No legacy preview or consent
is imported. No older native schema exists yet: missing manifest alone creates
an empty schema 1; all unsupported, unreadable or corrupt manifests block.
Their original bytes remain available for manual recovery.

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

Directories are mode 0700 and files 0600. Directory traversal uses no-follow
file descriptors; manifests, locks and artifacts must be single-link regular
files. Artifact names are derived solely from digests and artifacts are limited
to 8 MiB; the manifest is limited to 4 MiB. Every authoritative read validates
the entire typed graph and all referenced artifacts, including their byte hashes.
Duplicate JSON keys, nonfinite numbers and booleans as integers are rejected.
Worker acceptance and every phase change carry a checkpoint with operation,
owner and epoch. Validation replays the legal ordered history, requires the
current worker to match its last checkpoint and rejects missing effect intent,
phase downgrades, terminal resurrection and impossible owner/epoch changes.
Claim epochs form one contiguous, exclusive history across all workers; opening
an inconsistent manifest cannot release the uncertainty fence.
Artifacts and historical records are retained; garbage collection is deferred.
An unreferenced artifact or interrupted temporary file grants no authority.

One stable, separate `lock` inode is held with `flock` across read, graph
validation, revision comparison and atomic replacement. A process-local mutex
also serializes threads. Artifacts are flushed/fsynced and their directory
fsynced before a manifest can reference them. The manifest is written to a
private temporary file in the same directory, flushed/fsynced, replaced and its
parent fsynced. Errors after replacement are ambiguous acknowledgements:
reopen and replay the command, rather than assuming acceptance failed.

## Review, ownership and recovery

`preview` creates a new UUID with empty decisions and no approval. The canonical
approval binding covers the full identity, diff byte digest and submitted path
decisions. `review` requires a complete available observation of the exact
identity; `claim` checks it again, consumes that approval once, reserves the sole
worker and persists command acceptance before any future effect. One worker
fences every domain. Same command UUID and binding replay its durable worker
without changing revision, even in another process. Changed payload is refused.
Replay observes an existing result; it never launches work or restores consent.

The worker has an operation UUID, owner process UUID, monotonically increasing
epoch and independent external effect marker. Every worker mutation requires
the current revision, operation UUID, owner and epoch. Opening another store
or reading it never takes over a worker. `checkpoint(effect_started)` requires
another fresh complete observation immediately before a future adapter effect.
The adapter must make that durable checkpoint first, then perform its narrowly
scoped effect. This module does not implement the adapter or its race checks.

The first durable Preview in any domain atomically anchors its supplied scope
as the immutable baseline and active policy, with that candidate's retained UUID
as provenance. Only a truly empty store can have no policy. Cancellation,
superseding a candidate, refresh, restart and version update retain this anchor.
All later Previews must use the active policy; changing it requires a completed
scope publication. On every read, including stores with no scope transactions,
validation derives the active policy from that baseline and the ordered chain
of completed scope publications in exclusive worker epoch order. Policy
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
higher ownership epoch, retained old scope and uncertain-effect recovery.

Sources used: [Python 3.12 flock](https://docs.python.org/3.12/library/fcntl.html),
[fsync, replace and descriptor-relative APIs](https://docs.python.org/3.12/library/os.html),
[strict JSON considerations](https://docs.python.org/3.12/library/json.html#standard-compliance-and-interoperability),
[HA persistent App storage](https://github.com/home-assistant/developers.home-assistant/blob/master/docs/apps/configuration.md),
[Git expected-old-OID updates](https://git-scm.com/docs/git-update-ref.html),
[Git ref name validation](https://git-scm.com/docs/git-check-ref-format).
The architecture also compared [Terraform saved plans](https://developer.hashicorp.com/terraform/cli/commands/plan)
as an example of binding later execution to an explicitly reviewed candidate;
no Terraform code or execution assumptions were adopted.
