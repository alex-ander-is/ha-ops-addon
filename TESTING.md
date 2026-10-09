# Testing HA Ops

Run commands from the repository root with the dependencies pinned in
`requirements-dev.txt` (pytest 8.4.2, pytest-xdist 3.8.0, and PyYAML 6.0.3).

## Full verification

```sh
python3 -m pytest -n auto ha-ops/tests
```

The pre-push hook runs this entire suite. Focused groups below shorten the
development feedback loop; they do not replace that gate. `-n auto` already
uses the machine's physical CPU count. Workers are separate processes, which
isolates module globals and mocks; each real workflow uses disposable local
Git/config fixtures and fake Supervisor calls, never a live HA installation.

## Selectable groups

Every Python test file belongs to one of these groups. Counts are collected
pytest items including the preview decision guards (2746 total); remaining
`subTest` loops exercise additional cases within some items.

| Group | Files under `ha-ops/tests/` | Items | Coverage |
| --- | --- | ---: | --- |
| Server and workflows | `test_server.py` | 440 | Save/Apply previews, Git branches, conflicts, protected storage, rollback, cleanup, migration, state, transport, UI source contracts, diagnostics, and local harness safety |
| Backups | `test_backup_continuation.py` | 160 | Backup classification and creation policy, real Apply refusal/retry/acknowledgement, crash recovery, and HTTP/WebSocket replay |
| Client commands | `test_client_contract.py` | 22 | Durable command claims, concurrency, preview identities, stale selections, recovery, and client/server boundaries |
| Preview decisions | `test_preview_decisions.py` | 46 | Complete frontend module with inert Lit/Vaadin imports: local edits, content lifetime, synchronous final batches, refusal dismissal, real-operation fences, and native checkbox guards |
| Registry and layout | `test_registry_diff.py`, `test_heap_layout.py` | 10 | Semantic registry diffs, root heap discovery, and rejection of retired layout/settings |
| Native topology fixture | `test_native_topology.py` | 61 | Explicit Git root mechanics, exact index audit, blockers, persistence fixtures, Docker runner failure handling and topology workflow dependency coverage; real mounts require the separate runner |
| Native state | `test_native_operations.py` | 418 | Inactive durable candidates, bounded retention/checkpoints/policy provenance, generation replay, explicit schema-1 upgrade, exact streaming large artifacts, lifecycle, empty-store high-water integrity, corruption, crash/reopen and independent recovery pins; container replacement is a separate local runner probe |
| Native inventory | `test_native_inventory.py` | 252 | Dormant metadata-only byte-path inventory, scope-local diagnostics/blockers, both-phase stability and fallback proof, semantic parent safety independent of observed kind, retained parent/root acquisition failures, escaped LF/CR exclusion and scaffold fences, known-secret exclusions, component-fenced mounts and aliases, exact scope whitelist, separate effective-ignore/index audits, no-write/race/FIFO guards; real Linux mountinfo requires the local wrapper |
| Native Git adapter | `test_native_git.py` | 563 | Dormant fixed routing, kind-specific NUL byte records and real independent Git status axes/intent-to-add, complete tracked status with scoped untracked requests, exact bounded blobs and existing-object reuse proofs, complete selected/unselected blob dependencies and generated root/subtree destinations, ref CAS/uncertainty with malformed loose/packed storage and diagnostic refusal, complete private indexes with prior-evidence validation, no-checkout owned worktrees with bounded descriptors, verified sequential reopens, exact parent reservations and substitution non-adoption, real process interruption, kernel leases, foreign-resource preservation and durable addressed cleanup debt; full Unix byte identities require the local Linux wrapper |
| Native disk | `test_native_disk.py` | 649 | Dormant physical byte/inode accounting with user backups excluded; one actual immutable initial snapshot/forecast and phase scanner/capacity/Git sentinels, issued Snapshot/Plan refusal and context GC, bounded owner resource/state transitions and lifecycle fencing, exact raw marker/backlink controls, direct-ref alias refusal and pinned NativeGit replacement semantics, Repack membership fingerprints, private persisted journal/state and symmetric snapshot/current owner-lock presence and stable receipts with disclosed pre-first-observation limits, whole initial packed grammar and one explicit packed/peeled OID batch, descriptor-held proved absence versus acquisition uncertainty across initial/guard/phase Git/owner/output routes and monotone control presence, finite known-leaf loss refusal with real same/different-inode restoration and full Save reflog preimage retention, one initial artifact content verification, typed creation receipts retaining the original floor and nonblocking locks; real allocation, byte paths, mount aliases and process locks require the local runner |
| Local virtualization wrapper | `test_local_virtualization.py` | 103 | Setup, input isolation, exact bind sources, container ownership, and failure cleanup; real mounts require the local runner |
| Release safeguards | `test_release_readiness.py`, `test_pre_push_hook.py`, `test_release_script.py` | 22 | Release metadata, historical changelog placement, terminology, hook gate, release classification, tags, and failure rollback |

```sh
# Server and workflows
python3 -m pytest -n auto ha-ops/tests/test_server.py
# Backups
python3 -m pytest -n auto ha-ops/tests/test_backup_continuation.py
# Client commands
python3 -m pytest ha-ops/tests/test_client_contract.py
# Preview decisions (requires Node)
python3 -m pytest ha-ops/tests/test_preview_decisions.py
# Registry and layout
python3 -m pytest ha-ops/tests/test_registry_diff.py ha-ops/tests/test_heap_layout.py
# Release safeguards
python3 -m pytest ha-ops/tests/test_release_readiness.py ha-ops/tests/test_pre_push_hook.py ha-ops/tests/test_release_script.py
# Local virtualization wrapper
python3 -m pytest ha-ops/tests/test_local_virtualization.py
```

The smaller groups omit worker startup overhead. Each command can run alone
in a fresh Python process. For narrower iteration, select a class or name:

```sh
# Fast policy matrix without real Git Apply fixtures
python3 -m pytest ha-ops/tests/test_backup_continuation.py::BackupPolicyTests
# Save and Apply behavior inside the larger server group
python3 -m pytest -n auto ha-ops/tests/test_server.py -k 'save or apply'
# One independently collected backup counterexample (quote spaces/brackets)
python3 -m pytest 'ha-ops/tests/test_backup_continuation.py::test_real_retry_rejects_ambiguous_post_creation_inventory_in_either_order[duplicate slug before]'
```

The nine preview decision scenarios run the complete production frontend module
through Node. When Node is unavailable, they explicitly skip; those skips do
not verify frontend behavior. `HA_OPS_FRONTEND_SOURCE` selects a disposable
historical source for regression proof. Inert Lit/Vaadin imports test logic,
so these items do not establish DOM continuity, native focus, or appearance.

Browser smoke tests are a separate visual layer and are not collected by
pytest. `ha-ops/tests/browser/run.mjs` covers the local client/harness,
`ha-ops/tests/browser/backup-continuation.mjs` covers the backup footer flow,
and `ha-ops/tests/browser/preview-decisions.mjs` covers preview decisions.
See [browser instructions](ha-ops/tests/browser/README.md) for commands and
session behavior. Run the affected browser flow for visual changes; source
contract assertions alone cannot establish rendered behavior.

## Candidate native container topology

```sh
# Portable ordinary-directory fixture: Git mechanics and staging guards only
python3 -m pytest ha-ops/tests/test_native_topology.py
# Real Linux bind mounts, full container replacement, persistence and index audit
python3 ha-ops/tests/integration/run_native_topology.py
```

Native-tree step 3 has a separate dormant inventory contract:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_inventory.py
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_inventory.py
```

The Linux command is mandatory for step 3's actual `/proc/self/mountinfo`
same-device bind-boundary proof. A host-only skip does not establish that proof.
APFS's byte/case/Unicode filename limitations are exercised fully under Linux.
See [inventory API and acceptance/risk proof map](ha-ops/docs/native-inventory.md).
No production inventory import, workflow activation or live HA access is used.

Native-tree step 4 has a separate dormant Git adapter contract:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_git.py
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_git.py
```

Both commands use disposable Git/resource fixtures. The Linux command is
mandatory for invalid UTF-8, distinct case/normalization names and the full
byte-path index/tree/materialization proof. macOS executes parser/operation
checks and proves collision refusal before content writes; its narrower
filesystem set cannot replace Linux evidence. Mount-boundary cleanup uses real
kernel mountinfo plus a narrow same-device topology seam, without privileged
mounts. A real child with 120 selected files and soft `RLIMIT_NOFILE=64` checks all
reservations are empty before the first blob copy, exact bytes/modes and complete
cleanup. Descriptor failure and namespace/leaf mutation proofs retain original
errors and explicit addressed debt without adopting foreign entries. Grow/truncate
fixtures explicitly establish changed timestamp evidence; metadata-equivalent
roundtrips may be indistinguishable. Close double-fault guards preserve the first
exception/interruption, append secondary diagnostics and forbid unsafe FD retries.
Actual readback syscall guards cover selected content and owner/backlink evidence,
including single predecessor-close failure, leaf read/fstat/close and parent
open/fstat/final-close faults with balanced descriptors and addressed recovery.
Process-kill tests prove lease release, addressed recovery and explicit
retention when a file or marker was not durably acknowledged. Wrapper cleanup
failure makes the proof incomplete. See [native Git API and proof map](ha-ops/docs/native-git.md).
No production import, workflow activation, network or live HA write is used.

Native-tree step 6.5 has a dormant physical disk/accounting contract:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_disk.py
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_disk.py
```

The Linux command uses its default Linux `/tmp`; the output bind is virtiofs
and does not establish the supported ext4/tmpfs allocator profile. The low-space advisory case caps initial availability as an arithmetic seam.
Linux fixtures separately prove actual allocated blocks/free space, incompressible spool/blob
and no-reuse repack overlap, many tiny files, invalid byte names, shared-capacity
bind identities, same-operation native index phases, independent process
flock/death recovery and usable local admission. The read-only persistent root
prevents a real ext4 write fixture; that limitation is documented explicitly.
Host arithmetic seams do not substitute for Linux physical proofs. The group
also proves all exact file routes and creation receipts, canonical corruption
of both owner stores, zero unchanged-phase historical artifact content reads,
changed observation invalidation, one-shot issued contexts, no phase snapshot/forecast/capacity/Git enumeration, declared resource/output cardinality and length ceilings, same-owner lifecycle fencing, and bounded initial verification without
an artifact size cap. Timestamp mutation tests set an explicit changed mtime
so they do not depend on the local VM clock tick. Raw malformed-control tests
compare both actual owner parsers; exact canonical idle/active operation state
and idle/live journals retain their byte format with separate immutable
operation identity metadata. The NativeGit AST guard permits only the named
dormant NativeDisk dependency and detects flat, package-qualified and relative production imports of both, including aliases and nested imports.
Raw owner-marker variants compare actual NativeGit refusal with disk refusal for
real index/spool/worktree resources, including journal acknowledgement of new
noncanonical bytes. Recovery copies remain qualified; system-backup recipes,
receipts and attribution API have been removed from this dormant layer.
External writers can still cause actual ENOSPC/short writes. Reused isolated
NativeGit short-sink/publication/materialization/cleanup-debt and
NativeOperationStore fsync/replace/artifact-failure tests cover the existing
owner guarantees; dormant disk admission does not introduce a live executor.
No live
Home Assistant, production imports, executors, cleanup or activation is used.
See [disk API, executor profile and risk map](ha-ops/docs/native-disk.md).

Native-tree step 6.6 adds opt-in writable ext4 qualification without rerunning
the older native API groups:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_local_virtualization.py ha-ops/tests/test_native_ext4.py ha-ops/tests/test_release_readiness.py
python3 dev/virtualization/ha_ops_dev.py run --input . --ext4 --ext4-profile allocation --recreate --report dev/virtualization/.local/ext4-allocation-report.json -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_ext4.py
python3 dev/virtualization/ha_ops_dev.py run --input . --ext4 --ext4-profile bytes --report dev/virtualization/.local/ext4-bytes-report.json -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_ext4.py
python3 dev/virtualization/ha_ops_dev.py run --input . --ext4 --ext4-profile inodes --report dev/virtualization/.local/ext4-inodes-report.json -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_ext4.py
```

All three real profiles and allocation recreation are required; host skips
are not qualification. The development helper formats/mounts one bounded,
fully preallocated guest-native image outside the nonroot, capability-free
application container. Mandatory rows compare held physical byte/inode peaks
with one immutable NativeDisk forecast, including complete index/control/log
copies, materialization, independent recovery copies and no-reuse repack.
Independent observer telemetry stays separate from forbidden product rescans.
Actual byte/inode ENOSPC and scoped short/EIO owner failures preserve existing
content/state; addressed unacknowledged-resource debt is reported rather than
claimed removed. Exact cleanup, repeat cleanup and VM/image survival precede
passed reports. See [runner contract and measured evidence](dev/virtualization/README.md#guest-native-ext4-qualification)
for commands, profile boundaries and remaining future-executor obligations.

For the whole suite inside the local runner, the read-only input copy excludes
`.git` and the container's `/tmp` is not executable. Use a disposable copy under
`/workspace/output/checkout`, initialize and commit a fixture Git checkout,
provide fixture `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` and
`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL`, and set `TMPDIR` to a fresh directory
under `/workspace/output` so hook fixtures can execute. These are test-fixture
requirements; do not change the host checkout or production Git configuration.
Run the native-inventory group separately with the runner's default Linux `/tmp`
instead of the output `TMPDIR`: the output bind is backed by APFS, whose filename
rules cannot prove arbitrary Unix bytes and distinct case/Unicode names. The
other groups need executable fixture storage; this group needs Linux tmpfs.

The Docker runner is separate from pytest and the normal full-suite gate.
A successful real bind-mount run with full container replacement is mandatory
to complete native-tree step 1. The local Colima proof has passed; its report
is kept outside Git under `dev/virtualization/.local/topology-report.json`.
Use [the reusable local runner](dev/virtualization/README.md) for later tests.
It requires Docker on a local Unix endpoint backed by a Linux engine, and
network access during disposable image build to fetch Python/Git. Test runtime
uses no network and binds only freshly created temporary sources. Missing
Docker, daemon access or Linux engine returns nonzero; it does not count as a
successful skip. Every daemon command pins the validated endpoint and a frozen
environment, so changing the CLI's default context cannot redirect the test.
The runner attempts removal and absence verification independently for each
exact owned container and image, including after earlier cleanup errors;
aggregated cleanup failures fail the run. The fixture is removed after successful
resource cleanup. If resource absence cannot be confirmed, retain the fixture
and report its exact path and run ID for recovery.

`.github/workflows/native-topology.yml` runs the portable fixture,
release-readiness checks and the required Docker proof on Ubuntu. Both `push`
and `pull_request` use identical filters covering all `ha-ops/**` code,
configuration and build inputs, plus `repository.yaml`, `requirements-dev.txt`,
`TESTING.md` and the workflow itself. Topology dependency changes therefore run
the proof even when the fixture is unchanged. Its success artifact records
recreation, index audit and tool versions. Creating that
workflow locally does not run CI or authorize publication. See
[candidate topology and live inventory](ha-ops/docs/native-tree-topology.md)
for evidence boundaries and remaining migration blockers.

## Inactive native operation state

```sh
python3 -m pytest ha-ops/tests/test_native_operations.py
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_operations.py
# Full container replacement with the same disposable persistent data bind
python3 dev/virtualization/ha_ops_dev.py run --input . --recreate -- python3 ha-ops/tests/test_native_operations.py --container-recreation
```

This file belongs to the native state group and is collected by the full suite.
It checks inert state/transition behavior, strict corruption handling, artifacts,
real separate-process crash/reopen and competing ownership. The two-container
probe separately proves exact manifest/artifact persistence, a 17 MiB streamed
diff, advancing compacted frontier/generations, archived migration and an
independent recovery pin plus owner fencing across container replacement. Observations and outcome evidence are deterministic
fixtures; these tests do not verify future Git/live adapters or activate native
Save, Apply or migration. See [the state contract](ha-ops/docs/native-operation-state.md).

## Inventory review and parallelism

The October 2026 review covered all eight Python test files and both browser
flows. No test was proven obsolete or redundant, so all scenarios were kept.
An AST comparison found no identical Python test bodies. Similar checks often
cover different boundaries: backup policy classification versus real Apply
effects, source contracts versus rendered browser behavior, and state-store
claims versus HTTP/WebSocket dispatch. Retired layout rejection remains an
active compatibility boundary. The historical 0.8.54 changelog guard still
protects the placement of its correction.

Three slow backup `subTest` loops serialized 55 invalid-inventory cases,
14 ambiguous-created-backup cases, and 12 error-envelope cases into only
three schedulable items. They are now 81 independently collected cases with
the same payload families and assertions. The count rises from 534 to 612
without removing coverage. Each case gets its own temporary Git/config tree;
fixture cleanup also runs if setup fails. Timestamped payloads are built at
execution time and only stable case names enter collection IDs.

Use pytest for this suite: the three matrices are module-level parametrized
tests alongside existing `unittest.TestCase` classes. Pytest parametrization
does not work on `TestCase` methods. Short policy loops remain intact because
splitting them adds collection cost without addressing the measured tail.

## Measuring changes

```sh
python3 -m pytest -n auto ha-ops/tests --durations=15
python3 -m pytest -n auto ha-ops/tests/test_backup_continuation.py \
  -k 'invalid_inventory_families or ambiguous_post_creation or error_response_with_backup_data' \
  --durations=10
```

Compare sequential runs on the same machine, interpreter, worker count, and
command, with no competing test run. Report wall time as well as slow items;
more workers or more cases can increase Git setup and disk contention. The
initial focused comparison with ten workers was 18.22 s before the split and
15.76 s after it (81 scenarios in both). Splitting only the 14-case loop took
20.39 s because the 55-case loop still dominated. These are single-run
measurements, not a guaranteed speedup on other machines. The complete suite
on the same host took 94.25 s before this work and 53.54 s after it, using
`-n auto` with ten workers; compare fresh measurements when evaluating further
changes rather than treating those timings as a performance threshold.

For test maintenance, preserve each safety assertion and scenario until
redundancy is demonstrated at the same boundary. Check stable collection and
run focused groups independently after moving imports or fixtures. Run the
full suite for scheduling/isolation changes, and avoid rerunning it immediately
before an authorized push when the unchanged hook will run the same gate.
