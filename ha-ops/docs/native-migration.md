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
