# Dormant optional migration preparation (6.21)

`app/native_migration.py` is an explicit Python diagnostic API. Only the dormant Disk recipe validator imports it; no production
entrypoint imports it. It does not migrate files, initialize Git, create a native
operation, select scope, restore consent, run a worker, or activate native mode.
Steps 6.22–6.24 must obtain their own reviewed inputs and authority. A prepared
observation, including one whose inputs are current, grants none of them.

## Caller contract

Construct `Preparation(binding, app_identity=..., manifest_digest=...,
options=..., disk=None)` with a qualified `native_metadata.Binding`, current real mount
evidence, an explicit App/runtime compatibility identity, a SHA-256 commitment
to the resolved legacy manifest, and a callable for current direct-access
options. Construction is inert. The caller resolves legacy configuration first;
this API never guesses App slugs or reads credential-bearing options.

The default exact mapping is `homeassistant` to `homeassistant`. Additional
source-prefix/destination pairs must contain the actual resolved App slug or
an explicitly policy-allowed repository-file path. Paths are canonical Unix
bytes, encoded in review JSON as canonical base64. Component-overlapping source
or destination prefixes are rejected. Unmapped repository paths remain visible
as unresolved rows. `a` and `ab` are distinct components. Retired
`.storage_managed/core.config_entries.json` receives a retired descriptor; its
old/live file remains untouched. Both the original source identity and mapped
destination pass the known-secret policy before a content read, including
index-only paths missing from the checkout.

The native root and every acquired path use no-follow descriptors and confirmed
current mount routes. `/config`, when present, needs the existing explicit
canonical App mount alias evidence. Native repository metadata,
root-ignore-policy, repository-files and root `.git` anchors must be absent;
occupied anchors are retained as diagnostic blockers. This step does not
recognize or repair existing native canonical state. Synthetic mount evidence
is a portable host-test seam only; Linux uses current kernel mountinfo.

## Explicit methods and lifecycle

- `observe()` records two inventories and compares their canonical migration-input
  projections. Native diagnostic membership and payload churn have no input authority.
- `prepare(expected=...)` explicitly reobserves immediately before persisting.
  `expected=None` requires no existing candidate. Otherwise supply the digest
  returned by the last read; a detected concurrent candidate replacement raises
  `Conflict`. A competing admission mutex may refuse before candidate CAS.
- `read()` validates and returns persisted diagnostics, or `None` for absence.
  It does no source, Git, private-payload, worker or automatic validation work.
  Absence is returned only after safely acquiring the root and data anchors;
  unavailable anchors remain errors rather than empty state.
- `revalidate()` explicitly returns `absent`, `current`, `stale` or `unavailable`.
  Changed input durably marks the candidate stale. Unsafe/unavailable source
  observations also mark it stale when its store remains safely writable.
  Stale remains stale after A–B–A restoration. A new explicit preparation is
  required. An inaccessible store cannot promise a durable stale marker.

Restart, reopen, refresh and version update only expose old evidence. They do
not run source work or issue operation authority. The explicit App identity is
part of the observation: a different identity invalidates it during explicit
revalidation. Callers must change that identity on an incompatible version;
there is no implicit compatibility or consent rule.

## Review and completeness

Independent filesystem traversal records regular files, directories including
empty ones, unsupported symlink/special/hardlink leaves, ignored/untracked
files, nested Git boundaries and exact byte names. The legacy route cannot overlap fixed auth/service/operation/recovery/native
control/preparation namespaces. Known private directories
are visible as excluded boundaries and are not traversed. Eligible source regular contents and participating native destinations are streamed
into SHA-256 without a per-file size cap or retained payload. Native-only objects
carry explicit `diagnostic-only` reasons (with any unsupported-type detail), have
no content digest, and produce diagnostic layout rows. Their disappearance during
traversal is best-effort; unsafe routes and participating path failures still refuse. Numeric UID/GID, full `07777` mode, stat identity, size, mtime and ctime
are recorded; legacy-root metadata has its own binding field outside the real
filename namespace. No atime preservation is claimed.

Git's complete stage/sparse index, safely scoped status, explicit ignored and untracked
lists, symbolic/unborn/detached HEAD, loose/packed refs, service/remote/tag/stash
refs and Git pending controls are separate observations. Index OIDs/stages do
not become worktree or live digests. Index bytes and metadata remain untouched. Git status and ignored/untracked discovery receive only an explicit
list of eligible regular files and safely absent index-only paths, using literal
pathspecs with untracked traversal and rename detection disabled. Their persisted
status-path list discloses that exact contour. Excluded worktree payloads receive
only filesystem descriptors and independent index records; no Git child hashes
them. Directory/symlink/private-boundary paths are never discovery or status
pathspecs; every command uses the same exact approved leaf set.
Unsupported routing, sparse/split indexes, executable/included configuration,
corruption and Git failures retain explicit diagnostic issues. They never
become an opaque claim that a repository is clean or ready. Global/system
configuration and global excludes are disabled; only repository-local ignore
rules are inventoried. No claim is made about ambient user-global exclusions.

The structured diff covers mapped source paths, index-only/missing paths,
current eligible destination paths, unchanged rows and required destination
parents. Rows disclose before/after type, content digest and full metadata,
move/addition/deletion/type/content/metadata/unchanged distinctions, and explicit
excluded/retired/unresolved dispositions. A native-only deletion is a proposed
layout difference, never permission to delete. Source metadata has no invented
replacement for a missing index-only worktree object.

Legacy `/data/state.json`, native `/data/native-operations/state.json` and
`/data/native-services/state.json` are
observed separately through safe descriptors and bounded JSON parsing. Only
active/pending presence and worker/record counts enter the candidate, with a
pending blocker. Operation payloads, IDs, decisions, record content and grants
are not copied. The fixed `/data/native/recovery/active.json` presence is
recorded as an explicit unvalidated-recovery blocker: this step neither reads
its pointer payload nor follows generation/manifest/journal paths. Legacy command records count accepted/running/failed-unknown debt independently
of an active-operation pointer. Restored service history remains visible without
an active/pending blocker, including completed cycles with retained compensation
history. Current phase and dispatch debt determine activity. No operation,
service or recovery store is constructed or initialized.

## Private boundaries and persistence

Only `/data/direct-auth` and its fixed `tokens.json` stat descriptors are
observed. Token validation bytes and their hashes, installation/token IDs,
cookies, nonces, grants, CSRF state and TLS key contents are never read or
persisted. Missing auth stays missing. Direct configuration uses only
`direct_https`, `direct_origin`, `direct_certfile`, and `direct_keyfile`; the
candidate contains field names and one private configuration commitment, with
all values masked. Unknown options and repository URLs are never copied.
Fixtures prove remembered-cookie authentication and source/private byte and
metadata preservation after preparation.

The separate `/data/native-migration-preparation` store is private `0700`; its
single candidate is single-link regular `0600`, owned by the effective user.
No-follow bounded reads, exact keys, duplicate refusal, canonical byte codecs,
schema and content digests reject malformed or foreign state without repair.
Semantic validation also rejects null/duplicate refs, duplicate mount IDs,
nondirectory root/data/legacy identities and inconsistent metadata records.
The preparation directory's parent is fsynced on every explicit creation path,
including retries after an interrupted mkdir. Directory flock serializes independent processes; a digest CAS plus candidate
stat identity recheck protects replacement. Every load/save requires a registered
verified integer descriptor from the current anchored store lease; an absent
read result can never become ambient current-directory pathname authority. Temporary files use exclusive
creation and exact owned-inode cleanup. Complete writes precede file fsync,
atomic replacement and directory fsync. A directory fsync failure raises
`PersistenceUncertain`: the replacement is complete but its durability is
uncertain. Reopen validates it, never reports a partial success or deletes a
foreign replacement. Interrupted owned temporary files remain diagnostic
residue and are never automatically resumed or cleaned by `read()`.

Inventories are limited to 100,000 rows per contour; control/Git output and the
persisted document are bounded to 32 MiB, with ten seconds per read-only Git
command. Exhaustion is an explicit incomplete refusal, never truncation.
Regular content hashing remains streaming. Every source/root/Git/private
observation is repeated before publication. This detects cooperative races;
it is not an atomic filesystem snapshot against a hostile same-user writer.

## Evidence and qualification limits

The implementation reuses the installed Git plumbing, stdlib descriptor IO,
`native_inventory.Policy`, byte-path helpers and `Binding.check`. No added
package or generic migration engine is needed. Authoritative plumbing contracts:
[ls-files](https://git-scm.com/docs/git-ls-files),
[status](https://git-scm.com/docs/git-status),
[for-each-ref](https://git-scm.com/docs/git-for-each-ref), and
[repository layout](https://git-scm.com/docs/gitrepository-layout).

`test_native_migration.py` uses only isolated local fixtures. Linux is mandatory
for arbitrary byte paths, numeric owners and full special modes. Portable host
checks and generic Linux tests do not qualify the original App image, physical
native allocation, ACL/xattr restoration or any later filesystem transition.
There is no new UI/browser contour. See the focused commands in `TESTING.md`.

## Stable commitment and admitted persistence

Schema 2 stores an `input_digest` of one shared canonical projection used by the
observation double check, final preparation check and explicit revalidation.
Complete Git index-only mappings participate even when the checkout leaf is
absent. Mapped target roots and required ancestors retain device, inode, full
mode, UID/GID and mount identity. Only native directory size, link count and
mtime/ctime membership noise is normalized. Source directories and participating
regular-file contents and metadata remain strict. Diagnostic descriptors remain
in the saved review inventory; `current` validates inputs without refreshing that
inventory. Earlier schema candidates are refused rather than silently upgraded.
The projection is invariant under JSON serialization: required native-parent
`before` and `after` descriptors are normalized independently, while source
directory metadata remains exact. Excluded, retired and unresolved source rows
retain strict source/index evidence; their live diagnostic counterparts and
derived change labels do not enter the commitment.

Persistence defaults to a genuine same-root NativeDisk; an explicit participant
must use that root and its own issued layout/snapshot/plan. Serialize and validate
the complete bounded candidate and choose its exact `.pending-<32 hex>` address
before the one Disk snapshot. The explicit migration `storage_control` recipe
accounts old/new/temporary overlap, required directories, the shared admission
lock and failure reserves. No candidate mkdir/open/write/replace precedes guard
entry. Directory and completed temporary-file receipts bind executor effects;
targeted revalidation retains the initial forecast without charging own writes
again. The common Disk mutex is acquired before the preparation-directory flock.

Persistence requires qualified allocation/mount evidence and admitted idle
operation state. Unsupported filesystems, incomplete accounting, stale routes,
foreign locks and active or malformed competitors refuse before candidate writes.
An observation can still show safe redacted pending-operation diagnostics.
Both stale-marker branches use this same guard and one-snapshot path; an admission
denial returns `unavailable`, retaining the previous candidate bytes. Existing
CAS, owned-temp cleanup and post-replace `PersistenceUncertain` behavior remain.
The fixed mutex can bootstrap from an existing `data` directory without creating
`data/native`; it grants no migration execution authority.

## Dormant local transition (6.22)

`native_migration_transition.Transition` is a separate explicit Python
participant. Production server, startup and frontend do not import or dispatch
it. Construction and `read()` only expose saved evidence. A candidate is still
diagnostic: a caller must request `confirm(candidate=..., operation=...,
action='start', approved=True)` and pass its one-use, issuer-bound in-memory
confirmation to `start()`. Use a fresh 32-character hexadecimal operation ID.
Confirmation binds current App, manifest, policy, root, mount routes, direct
configuration commitment and auth descriptors. Start explicitly revalidates the
candidate before taking the shared admission mutex, then compares a pure current
observation and candidate CAS under that mutex; no nested Preparation write
path runs while admission is held.

`read()` never repairs, creates, cleans or resumes. Reopen, refresh, process
restart and App update never restore confirmation. Explicit `resume()` requires
a newly issued confirmation with `action='resume'` and the exact saved operation
and candidate. An incompatible App/runtime, changed direct configuration/auth,
source/live descriptor drift, replaced output or ambiguous namespace refuses.
Completed state grants no remote publication or native activation authority.
There is no automatic cleanup, cancellation, rollback or deletion API.

The private `data/native-migration-transition` store has mode `0700`; journal,
partial controls and backup payloads have mode `0600`. The journal contains only
fixed-address actions, canonical byte paths, complete source commitments,
progress and inode receipts. It rejects duplicate/unknown keys, forged actions,
invalid phases/sequences and unsafe descriptors. Its complete prospective
receipt envelope must fit 8 MiB before transition effects. Metadata and targeted
preservation projections are limited to 20,000 rows; payloads are streamed,
without an arbitrary repository-byte ceiling. Git observations have the existing
32 MiB output and ten-second deadline bounds. Exhaustion refuses completeness.
One fixed `journal.pending` fragment bounds interrupted publication storage
without a retry ceiling. Read exposes the intact canonical phase and retains
the fragment. An explicit admitted worker settles only an exact prefix of its
intended journal payload, with traced unlink and parent fsync before exclusive
recreation. Foreign or incompatible fragments remain retained and refuse.

The narrowly issued NativeDisk participant uses the existing process/thread
mutex and flock before the transition directory lease. One real accounting
snapshot charges backup/native payloads, every pending output inode, allocation
rounding, extent/admin overhead, complete journal overlap and failure reserve.
Already receipted outputs remain in current allocated storage; resume projects
only remaining output and verifies any partial prefix before capacity credit.
The original metadata already consumes current kernel capacity. Retirement is
an atomic no-replace same-filesystem rename and does not allocate another copy.
Unsupported allocation profiles and cross-filesystem retirement refuse.

The executor first retains a full independent raw metadata backup, including
config, hooks, every object/ref/reflog and the exact index or its absence. The
private journal preserves original numeric file attributes separately from
private backup access modes. It streams and independently reads back every
payload, compares complete source pre/post commitments, verifies refs/object
format, runs read-only `fsck` against the backup and native image, and checks
staged object accessibility. Only a durably acknowledged recovery verification
allows old `checkout/.git` to move into retained `retired.git` escrow. Both raw
backup and original escrow remain available. A bundle or reconstructed index
cannot substitute for this recovery evidence.

The native image copies objects, HEAD, loose/packed refs and logs, with generated
minimal config and an absent native index. It does not transplant old hooks,
remote URLs, includes or filters. It publishes only through Linux
`renameat2(RENAME_NOREPLACE)`; existing or raced destinations are retained.
Canonical metadata is `data/native/repository.git`, explicitly paired with the
root worktree. Root `.git` remains absent. Exact previously absent
`repository-files`, saved `root-ignore-policy` and root `.gitignore` provide a
deny-all scaffold with no selected live content. Preexisting output anchors
refuse rather than being overwritten.

Every executor mutation and persistence syscall passes through its action trace:
exclusive create, each short-write continuation, mkdir, directory/file fsync,
journal replacement, no-replace rename and checkout mtime restoration. Pending
intents reconcile an already completed rename by exact inode or continue a
verified private prefix under explicit resume; destructive renames are never
repeated. Before the first journal payload, a fixed private `bootstrap.id` durably records
the operation identity. If no canonical journal exists, public `read()` exposes
`bootstrap-pending` diagnostics with the complete operation ID, or `None` for
an empty/partial hexadecimal marker. This restores no permission: a fresh
explicit start revalidates the candidate and addresses the exposed complete ID.
An explicit admitted worker may settle an incomplete hexadecimal marker only
when both canonical and pending journal are absent, proving that no payload or
transition action could have begun. Foreign bytes and inconsistent fragments
remain retained/refused. Repeated marker opens/writes/fsyncs and settlement
unlink/parent-fsync boundaries are traced. Recovery does not depend on a vanished
process remembering its UUID. This assumes cooperative writers under
the common lock, not a hostile process with the same user identity. A partial
output with foreign content, mode or identity refuses. Before retirement and
publication, and on explicit resume after publication, the entire frozen native
image membership is revalidated; unknown refs, objects, logs, files or empty
directories cannot be adopted by a prior verification receipt. Journal replacement with
failed parent fsync raises persistence uncertainty; it never reports completion.

All nonservice children remain untouched, including live content, untracked and
ignored files, auth state and nested repositories outside source boundaries.
Source nested Git boundaries remain 6.21 blockers: confirmation refuses with
zero transition effects rather than relaxing that safety rule. Independent test
inventories check bytes, inode, link topology, modes, numeric owners and
nanosecond mtime/ctime at every injected boundary. Exact containing service
parents legitimately change membership. Checkout parent ctime changes when its
`.git` leaves; its mode/owner/inode remain and its mtime is explicitly restored.
No atime invariance is claimed.

Terminal phase is `local-transition-complete-awaiting-review`. Step 6.23 must
obtain separate diff approval, construct the layout-transform commit with old
ancestry, route any still-active legacy workflow to retained metadata and
reconcile publication. Step 6.24 owns root policy reconstruction after container
replacement and activation. Current Save/Apply is not wired to this participant;
an absent native index does not mean historical path layout is ready for it.
Linux process-interruption proof does not qualify physical power loss or replace
the original App/runtime/ext4 qualification boundary.

## Dormant Git layout candidate (6.23)

`native_migration_git_candidate.Candidate` is an explicit local API. It requires
one completed transition, the exact saved preparation and a direct frozen
`refs/remotes/...` commit. It does not fetch or establish remote-server freshness.
The tracked parent tree supplies content; staged, working, untracked and ignored
checkout bytes remain retained separately. The new commit has that single parent.

Every eligible parent leaf needs an explicit mapping. Declared
`Policy.repository_files` leaves remain Git objects without live materialization;
unknown leaves refuse. Retired and denied leaves have index-only removal rows.
Managed metadata comes from frozen regular source descriptors and exact required
parents, preserving numeric ownership, full modes and owner-execute semantics.
The generated metadata manifest and literal allow rules are candidate objects;
the installed deny-all policy remains unchanged.

The builder copies object storage into an independent private native root below
`data/native-migration-git-candidate/<operation>/root`. It uses existing Git
plumbing, an empty isolated index and an exact deterministic commit identity.
Canonical native, retired and recovery images are revalidated without mutation.
Complete before/after rows, unchanged leaves, full metadata and content references
are digest-bound in `candidate.json`. `read_content` streams either side from the
verified private object database; binary bytes are never truncated into a preview.

Construction requires one issued NativeDisk recipe, one accounting snapshot and
one data-volume capacity observation before any candidate output. Full copied
history, new Git objects, index/spool/journal resources, control overlap, inodes
and retained failed generations are charged without allocation credit. Fewer than
500 MB free remains advisory when the conservative forecast fits. Candidate
checkpoints have a separate opt-in ext4 proof; host arithmetic tests do not
qualify physical allocation or power-loss behavior.

Publication of the diagnostic pointer uses a fresh addressed `.pending-<token>`
control, exclusive creation, complete readback, CAS and file/directory fsync.
Interrupted generations and pending fragments remain explicit charged evidence;
fresh explicit operations can proceed, but addressed incomplete operations are
never resumed implicitly. Directory-fsync failure reports uncertain persistence.
No automatic cleanup or disposition API exists in this step.

Read/reopen/refresh/restart never rebuild, resume, clean up or restore consent.
Saved review stays immutable. Explicit `revalidate()` proves current local inputs;
observed drift persists a sticky stale marker, including across A-B-A changes.
A durable `validation: pending` pointer precedes every freshness observation.
A crash or failed final write leaves it unavailable across restart, even after
inputs return to old values; only a fresh explicit build can replace it. Admission
failure performs no freshness observation. The immutable `candidate` digest binds
the complete review; a separate pointer digest supplies CAS for diagnostic state.
Unavailable stale persistence returns `unavailable`. An App identity update keeps
diagnostics but requires revalidation; a saved `stale: false` is never authority.
There are no approval, publication, activation, server or frontend entrypoints.
