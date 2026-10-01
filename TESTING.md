# Testing HA Ops

Run commands from the repository root with the dependencies pinned in
`requirements-dev.txt` (pytest 8.4.2 and pytest-xdist 3.8.0).

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
pytest items after splitting the backup matrices (612 total); remaining
`subTest` loops exercise additional cases within some items.

| Group | Files under `ha-ops/tests/` | Items | Coverage |
| --- | --- | ---: | --- |
| Server and workflows | `test_server.py` | 425 | Save/Apply previews, Git branches, conflicts, protected storage, rollback, cleanup, migration, state, transport, UI source contracts, diagnostics, and local harness safety |
| Backups | `test_backup_continuation.py` | 135 | Backup classification and creation policy, real Apply refusal/retry/acknowledgement, crash recovery, and HTTP/WebSocket replay |
| Client commands | `test_client_contract.py` | 20 | Durable command claims, concurrency, preview identities, stale selections, recovery, and client/server boundaries |
| Registry and layout | `test_registry_diff.py`, `test_heap_layout.py` | 10 | Semantic registry diffs, root heap discovery, and rejection of retired layout/settings |
| Release safeguards | `test_release_readiness.py`, `test_pre_push_hook.py`, `test_release_script.py` | 22 | Release metadata, historical changelog placement, terminology, hook gate, release classification, tags, and failure rollback |

```sh
# Server and workflows
python3 -m pytest -n auto ha-ops/tests/test_server.py
# Backups
python3 -m pytest -n auto ha-ops/tests/test_backup_continuation.py
# Client commands
python3 -m pytest ha-ops/tests/test_client_contract.py
# Registry and layout
python3 -m pytest ha-ops/tests/test_registry_diff.py ha-ops/tests/test_heap_layout.py
# Release safeguards
python3 -m pytest ha-ops/tests/test_release_readiness.py ha-ops/tests/test_pre_push_hook.py ha-ops/tests/test_release_script.py
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

Browser smoke tests are a separate visual layer and are not collected by
pytest. `ha-ops/tests/browser/run.mjs` covers the local client/harness, and
`ha-ops/tests/browser/backup-continuation.mjs` covers the backup footer flow.
See [browser instructions](ha-ops/tests/browser/README.md) for commands and
session behavior. Run the affected browser flow for visual changes; source
contract assertions alone cannot establish rendered behavior.

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
