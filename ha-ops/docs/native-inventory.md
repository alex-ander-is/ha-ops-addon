# Native inventory and managed scope: step 3

`app/native_inventory.py` is dormant. It has no production startup, server,
UI, state-store, or legacy Save/Apply integration. It returns immutable
diagnostics and in-memory ignore bytes; it never stages, repairs policy,
changes refs, removes live paths, or persists scope. Refresh, restart and
version updates retain no inventory or scope state. A caller must request a
fresh inventory. These observations cannot authorize later writes.

## Identity and traversal

`normalize_scope` uses exact Unix bytes, rejects noncanonical relative paths,
and collapses component ancestors. `foo` never includes `foobar`. Case,
Unicode normalization forms, invalid UTF-8, whitespace, backslashes and
literal glob characters remain distinct. Strings use `os.fsencode`; display
text is not identity. LF/CR names remain in inventory and index diagnostics,
but `render_ignore` refuses them because line-based allow rules cannot safely
represent them. It returns no partial policy.

`inventory(root, selected, policy=..., mounts=..., confirmed_mounts=...,
topology_verified=...)` checks the root and every directory component with
descriptor-relative no-follow opens. It enumerates and stats entries, without
opening file contents, hashing files, parsing configuration or reading xattrs.
Symlinks and special files are diagnostic leaves and cannot be selected.
Hardlinks retain each distinct path, inode and link count. Entry sizes are
metadata sizes, not directory aggregates or projected Git storage requirements.
Traversal is iterative and closes directory descriptors before the next
directory. Membership, metadata and root namespace rechecks report failures
and races explicitly, never as an empty successful scope.

`parse_mountinfo` preserves mount IDs, parent IDs, filesystem roots, mountpoints,
device fields and sources, including documented octal byte escapes. Device
equality does not establish mount topology. A root requires confirmed mount
evidence. A descendant boundary requires its own confirmation for every entry
kind; unconfirmed regular-file mounts cannot become managed files. Neither
reserved directory scaffolding nor repository-only exceptions can cross an
unconfirmed boundary. Ambiguous stacked records and malformed mount paths block.
`linux_mounts()` reads `/proc/self/mountinfo`.
Every inventory descendant acquisition checks each component's unique confirmed
mount before stat/open. The shared guard covers preliminary alias probes, the
main scan and final namespace rechecks; a confirmed deeper mount cannot bypass
an unknown or stacked ancestor. The initial root descriptor and its final
identity check are diagnostic anchors, not permission to descend. Separate
policy-copy and Git audits retain their explicit-location contracts.
Injected records are a portable test seam; `topology_verified` is caller-owned
provenance, not verification performed by a boolean. Kernel evidence and
reviewed confirmations are required for an actual Linux claim.

Only `homeassistant`, `addon_configs` and explicitly reviewed additional live
roots are eligible. `config` remains an excluded alias row pointing at
`addon_configs/<self_slug>`. Its confirmation requires distinct confirmed
mount IDs, matching filesystem-relative source/root mapping and directory
identity. It is never rewritten into canonical selection. Denied directories
remain visible without enumerating their contents. Explicit selection of a
denied root blocks; a selected eligible ancestor omits denied children.
An absent alias is optional in a synthetic fixture; a present alias with an
absent or disappearing canonical target explicitly blocks confirmation.

## Finite known-secret policy

The policy is independent of `EXPORT_EXCLUDES` and `EXPORT_CLEAN_PATHS`.
`POLICY_VERSION` is `native-known-secrets-v1`; `POLICY_DIGEST` binds the built-in
matrix, and `Policy.digest` additionally binds self slug, legacy checkout,
configured denials, additional live roots and repository-only files.

| Path rule | Diagnostic reason |
| --- | --- |
| Any `secrets.yaml` component within an eligible config domain | `known-secret-secrets-yaml` |
| `.storage/core.config_entries`, `.storage/auth`, `.storage/auth_provider.homeassistant`, including descendants | `known-storage-credentials` |
| `.cloud` subtree or `.google.token` component in a config domain | `known-cloud-credentials` |
| `.ssh`, `.gnupg`, `.git-credentials` components in a config domain | `known-credentials` |
| `zigbee2mqtt/coordinator_backup*.json`, including descendants | `known-coordinator-backup` |
| Any `.git` component | `git-metadata` |
| `config`, `backup`, configured `data/<legacy checkout>` | Alias, backup or legacy-checkout exclusion |
| `data` except exact configured files below `data/native/repository-files/` | `service-data` |
| `proc`, `sys`, `dev`, `run`, `tmp`, `bin`, `sbin`, `usr`, `lib`, `lib64`, `etc`, `root`, `var`, `opt` | `container-system-root` |
| Other root paths | `non-managed-root` |

Additional deny paths extend this matrix. Additional roots cannot reopen
reserved/system paths. Repository-only exceptions open only exact files;
their directory scaffolding never permits selecting `data`, private metadata,
options, keys, state, diffs, recovery, logs or the old checkout.

Whole device/entity registries remain eligible. Other `.storage` records emit
`unclassified-storage-content`; they are not certified secret-free. This is
known-path exclusion, not detection of arbitrary embedded secrets or every
hardlink to secret content. No secret contents are inspected.

Manually selected databases, WAL/SHM files, logs, caches, `node_modules`, media
and large regular files remain eligible. Runtime/large-file warnings inform
later review; there is no admission ceiling. Disk admission is step 5 and the
confirmation UI is step 16.

## Three separate checks

`render_ignore(snapshot, policy)` requires a complete snapshot. It denies
everything, reopens exact eligible enumerated files and `/.gitignore`, and
reopens required parents while denying their other children. Literal pattern
escaping preserves spaces, tabs, backslashes, `!`, `#`, `*`, `?` and brackets.
New unenumerated siblings remain ignored. Returned bytes are never written.

`audit_policy_copies(root, expected)` checks root and saved policy copies through
single-link regular no-follow descriptors and compares exact bytes. Missing,
empty, inconsistent, linked, replaced or inaccessible files block. There is no
automatic repair. Pass the expected bytes from the same policy/snapshot.
Leaf opens are nonblocking, then verify the opened descriptor is the same
single-link regular file before reading. A regular-to-FIFO replacement returns
a blocker instead of waiting for a FIFO writer. Directory opens use
`O_DIRECTORY`; Git subprocesses retain a timeout even if metadata becomes a
special file after its preliminary validation.

`audit_ignore(root, paths, snapshot, policy)` checks effective Git rules with
`check-ignore --no-index --stdin -z --verbose --non-matching`. Include inventory
rows and relevant index paths; the immutable snapshot supplies exact eligible
files and required traversal parents. A lower-level scope-only argument is a
parser/test seam for path-policy candidates; it lacks the exact enumeration
needed to judge empty directories. NUL tuples preserve byte identities,
nonmatches and negations. Nested `.gitignore` can both expose prohibited files
and hide selected files; matching root-policy bytes cannot excuse either.

`audit_index(root, scope, policy)` independently reads the entire
`ls-files --stage -z --sparse` index, with no pathspec or exclude filter.
`--sparse` keeps sparse directory records explicit for rejection. Every record
is checked against component selection and known denials, even when absent
from live inventory. Pending deletion of an allowed tracked file is valid;
force-added secrets and unselected byte names block. Nonzero merge stages,
duplicate/conflicting entries, symlinks, gitlinks, sparse entries, unsupported
modes and malformed records block. It does not require every eligible file to
be tracked. A clean ignore report never pardons an unsafe index.

Both Git commands use explicit worktree/metadata locations, bounded subprocess
timeouts, sanitized `GIT_*` variables, optional locks disabled, global/system
config isolation, disabled hooks/fsmonitor/attributes/global excludes, and
no-follow metadata-directory validation. Routed metadata and linked index,
config or HEAD files are refused. They execute no Git write command.
Namespace rechecks are diagnostic race detection, not an atomic snapshot or
a sandbox against a hostile same-user process. Future adapters must recheck
each write and provide their own routing/race fences.

## Acceptance and risk proof map

All new tests are in `tests/test_native_inventory.py`.

| Obligation | Durable regressions |
| --- | --- |
| R001: exact byte identities and component normalization | `test_noncanonical_paths_rejected`, `test_component_selection_bytes_and_immutability`, real literal Git and complete-index tests |
| R002: finite secret matrix without legacy runtime exclusions | Parametrized secret/whole-file matrices, sparse 10-GiB metadata fixture, repository-only denies |
| R003: no leaf reads or dereferences; explicit failed/raced scans | No-follow sentinel, changed-child including FIFO directory replacement, namespace-failure, policy-replacement and bounded root/saved policy FIFO-race tests |
| R004: explicit same-device mounts | Mount parser, malformed injected evidence, all entry-kind/scaffolding boundaries, alias no-open guards for unknown/stacked ancestor/self and confirmed child under unknown ancestor, shared scan/recheck component guard, and real Linux bind proof |
| R005: canonical alias, hardlinks, reserved roots | Alias mismatch, present-alias/missing-canonical blocker and mapped-source identity fixtures, hardlink metadata, reserved-root/repository-file cases |
| R006: exact whitelist and both nested override directions | Real Git literal rules, empty scope, tracked `check-ignore --no-index`, nested override tests |
| R007: independent complete index, merge stages/modes/deletions | Force-added LF/invalid-byte fixture; real Git symlink/gitlink and unmerged stage fixtures; malformed/sparse-mode seams |
| R008: complete effective-ignore parsing and command failures | Four-field, nonmatch, negation, duplicate/missing/malformed tuple, exit-status and timeout tests |
| R009: isolated routing and policy copies | Hostile environment, fsmonitor sentinel, external excludes, linked metadata, exact single-link/nonblocking policy tests and static/raced index/config/HEAD FIFOs with bounded Git invocation |
| R010: dormant lifecycle, fresh scope, resources | Consecutive broad/narrow/empty results, immutable records, deep-tree descriptor cleanup, no-production-import guard |

Before/after file hashes and modes include disposable Git metadata and policy
copies. They prove diagnostics and scope recomputation do not write the fixture.
The Linux proof scans only the runner's `/workspace` namespace: distinct
`input`/`output` bind IDs on one device, blocked unconfirmed traversal, and a
fresh output file after explicit confirmation. Input contents remain excluded.
Synthetic alias identity tests do not replace step 1's real alias topology proof.

## Verification and evidence

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_inventory.py
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_inventory.py
```

Portable macOS cannot create all invalid UTF-8/case/Unicode-distinct filesystem
names; its byte algorithms still run, while Linux runs the full identity fixture.
The real mount test explicitly skips outside the local Linux runner. That skip
never completes the Linux proof requirement. Development runtime: Python
3.12.10 and Git 2.39.5; host portable runtime: Python 3.9.6, Git 2.39.5.
Production runtime/activation and full container migration remain unverified.

Executed October 6, 2026 after correction 2: host focused suite **111 passed,
1 explicit Linux mount-proof skip**; isolated Linux focused suite **112 passed,
no skips**. The final Linux run printed input/output mount IDs **317 / 318**,
both device **0:41**. `git diff --check` passed. The foreman owns the independent tester
passes and final full-suite gate; this focused result does not claim those
later checks passed.
The focused Linux command uses default Linux `/tmp`. Other baseline groups
require executable fixture `TMPDIR` under the output bind; that APFS-backed
storage cannot prove arbitrary byte/case/Unicode names, so the groups run with
different fixture roots as documented in `TESTING.md`.

Implementation follows the accepted October 6, 2026 architecture evidence:
[Python descriptor/byte APIs](https://docs.python.org/3.12/library/os.html),
[Linux mountinfo](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html),
[Git ignore precedence](https://git-scm.com/docs/gitignore),
[check-ignore NUL format](https://git-scm.com/docs/git-check-ignore),
[complete/sparse index records](https://git-scm.com/docs/git-ls-files), and
[Git configuration](https://git-scm.com/docs/git-config).
The architect compared maintained
[restic 0.19.1 filesystem/backup contracts](https://restic.readthedocs.io/en/stable/040_backup.html)
and its BSD-2-Clause filesystem abstraction. No code or dependency was copied;
mutable current source was not used as a production compatibility guarantee.
