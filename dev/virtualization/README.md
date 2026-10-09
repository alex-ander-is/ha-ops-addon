# Local Linux test runner

`ha_ops_dev.py` owns a persistent Colima profile named `ha-ops-dev`. It is for
development and tests only; nothing under this directory is copied into the
HA Ops App image. The profile, its Docker images, and package cache survive
test runs. Disposable bind fixtures live in ignored `.local/runs/` and are
removed after each run. No command here uses the live Home Assistant host.

## First use

On macOS with Homebrew and at least 30 GiB free on the workspace volume:

```sh
python3 dev/virtualization/ha_ops_dev.py setup
python3 dev/virtualization/ha_ops_dev.py topology
python3 dev/virtualization/ha_ops_dev.py status
```

`setup` installs missing Colima and Docker CLI through Homebrew, checks their
versions against `toolchain.json`, starts a
2-CPU, 4-GiB-memory VM with a 12-GiB Docker disk and 8-GiB root disk, verifies its exact local Unix
socket, and builds a fingerprinted image only when its inputs change. It
smoke-tests the image on every call. The VM is provisioned with only this
directory as an explicit writable host share. The wrapper leaves the active
Docker context untouched and pins every Docker operation to the named socket.
Run `setup` again after changing the Dockerfile, pinned Python requirements,
topology helpers, probe, host architecture, or Docker/Colima version. A new
fingerprint gets a new image; older images are retained for explicit review.

The initial 30-GiB threshold budgets 12 GiB for the Docker disk, 8 GiB for
the root disk, 2 GiB for image build/download headroom, and an 8-GiB host
reserve. Existing-profile setup requires 10 GiB free, and each run requires 10 GiB free. The VM Docker volume
must have at least 2 GiB free. These are admission checks, not a promise that
macOS or Homebrew will use a fixed amount. Check `status` and available disk
space if a later run is refused. Never use a broad Docker prune for this tool.

`topology` runs the real two-container bind/recreation proof and writes an
ignored `.local/topology-report.json` with the observed versions, capacities,
architecture, image ID, and proof result. It creates all five exact bind
sources and checks their bytes from the named VM before Docker builds an image
or starts even the smoke container. The reusable runner keeps no-follow
descriptors for the run root and bind directories across endpoint preflight,
source verification, and Docker creation, refusing a replaced path. The Linux
CI runner keeps its independent Docker contract and image lifecycle.
Reusable topology skips build-context creation and copying entirely. Cleanup
traverses held directory descriptors and refuses a replaced root; parent runs
hold their root from creation and tolerate the child removing that same root.
The host cleanup uses Python 3.9-compatible APIs.

The private, unpredictable run directories are local same-user fixtures.
They are not a sandbox against a hostile process running as the same user:
deliberate replacement before the child acquires its descriptors, or during
source-marker check/write, remains outside the isolation guarantee. Manual
cleanup acquires the current exact run directory when invoked; it cannot
recover the directory identity held by an earlier, interrupted process.

## Later tests

```sh
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q ha-ops/tests/test_native_topology.py
python3 dev/virtualization/ha_ops_dev.py stop
python3 dev/virtualization/ha_ops_dev.py cleanup --run EXACT_RUN_ID
```

`run --recreate` repeats the same command in a second, newly created container
against the same disposable output bind. It verifies removal of the first
exact owned container before creating the second, and preserves all ordinary
network, mount and cleanup guards. The command can detect its first/second
phase using only a marker in `/workspace/output`; default `run` executes once.
Both containers and the fixture are removed on success.

`run` copies one repository-relative input from this local, credential-free checkout into a fresh fixture. Use `--input .`
for tests that resolve files from the repository root, including the topology
unit tests above. It mounts the
copy read-only at `/workspace/input`, and provides only `/workspace/output` as
a writable bind. It runs without network access, with a read-only container
root and a temporary `/tmp`. The command is executed directly, without a host
shell. The wrapper inventories the complete input before starting the VM or
using Docker. It copies all regular files regardless of name or extension,
including `settings.yaml` and unfamiliar file types. It does not inspect file
contents for keys: the local checkout is the trusted source and is kept
credential-free. Only Git and generated control/cache paths are excluded. It
rejects paths outside this repo, symlinks, hard-linked files, special files,
and mounted descendants. The copy reads each file through a descriptor
anchored at the repository root, opens every path component without following
symlinks, and checks file identity and directory entries before and after
reading. A changed source discards the
whole copy before any container bind. The fixture is copied and its exact VM
share checked before any Docker image build or smoke container. Output files in the disposable fixture are removed
at the end; commands should print results to stdout. For tests needing more
inputs, create a dedicated repository fixture directory and pass it as
`--input`. Runtime network access or access to credentials, the Docker socket,
live HA paths, or the host root is deliberately unsupported.

`status` reports stopped or broken Colima profiles without assuming a stale
socket is live. When process inspection says `Broken`, it checks the named
endpoint read-only; a verified engine is reported as running, while denied
socket access is reported as unverified. A responding engine with the wrong
identity, architecture, or pinned version is never stopped for recovery. Run it with normal local host access
when using a restricted development sandbox. `setup` attempts a non-destructive
stop/start recovery only after a broken endpoint is confirmed; denied access
refuses recovery, and the profile and disks are never deleted. `stop` leaves the VM
disk and images intact. `cleanup --run` removes only
resources bearing that exact run ID; ordinary successful runs clean themselves.
If a process is interrupted, inspect the run ID on its labeled container and
use exact cleanup. Cleanup attempts every container with the exact run ID and
reports all failures while leaving a failed run's fixture for inspection. Do
not delete the VM or shared `.local` state to recover a
failed run.

Apple Silicon `arm64` is the primary tested host; Colima provisions an
`aarch64` Linux guest and Docker selects the matching multi-architecture base
image. Intel macOS `x86_64` is supported by the same profile settings but
requires its own successful local proof. Cross-architecture emulation is not
enabled. The image starts from the digest-pinned
`python:3.12.10-slim-bookworm` base, with the exact Python test dependencies
in `requirements-dev.txt`, including PyYAML for backend imports. The development
Dockerfile also pins rsync `3.2.7-1+deb12u6` for disposable Preview workflows.
`toolchain.json` pins Colima 0.10.3, Docker CLI
29.8.2, Docker Engine 29.5.2, and guest Git 2.39.5 (Debian package
`1:2.39.5-0+deb12u3`); a version change needs
a reviewed toolchain update and fresh image proof. Observed host versions are
recorded in the local report. Before installing, the wrapper checks that
Homebrew currently offers the pinned formula versions; it refuses an
unavailable pin without changing the host. The repository does not
alter Homebrew update policy or install a global Docker daemon.

The image tag is calculated from one immutable in-memory tar archive passed
unchanged to Docker through stdin. Build inputs use the same repository-anchored
descriptor checks as test inputs. The toolchain pins are loaded from those
verified archive bytes before installation or VM setup; importing the wrapper
never reads a build input. A new image builds under a temporary tag;
the wrapper assigns the reusable fingerprint tag from those exact archive bytes
and verifies removal of the temporary tag. A cleanup failure reports its exact
tag for recovery. The wrapper verifies a host-to-guest and guest-to-host byte round trip through
Colima SSH before Docker mutation. It uses Docker `--mount` and checks every
created container's reported source, type, destination, and access mode.
These checks matter because Docker bind paths are resolved on the daemon host,
which is the Colima VM on macOS. See the upstream [Colima configuration][1],
[Colima start flags][2], [Docker bind-mount documentation][3], and
[Docker tar-context documentation][4].

[1]: https://github.com/abiosoft/colima/blob/main/embedded/defaults/colima.yaml
[2]: https://github.com/abiosoft/colima/blob/main/cmd/start.go
[3]: https://docs.docker.com/engine/storage/bind-mounts/
[4]: https://docs.docker.com/build/concepts/context/#local-tarballs

## Metadata candidate evaluation

The development image pins etckeeper `1.18.20-1`, Metastore `1.1.2-4+b1`,
acl/libacl1 `2.3.1-3` and libbsd0 `0.11.7-2`. Etckeeper is extracted under
`/opt/metadata-tools` without maintainer scripts, package-manager hooks or
initializing `/etc`; this is an evaluation dependency only. Python inspects
binary xattrs without installing attr. Rebuilding through `setup` needs the
normal dependency/build-network approval and retains older reusable images.

```sh
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_metadata_evaluation.py
```

This root fixture uses generic runner `/tmp` tmpfs with existing narrowly
bounded capabilities, network none and a read-only root. It verifies actual
tools, deleted-inode Git revert, independent Git transport, exact readback,
selection coupling and candidate defects. The output bind and nonroot ext4
profile are not metadata proof substitutes. Missing Linux requirements fail;
macOS skips are collection evidence only. See [observed candidate results and
remaining limits](metadata-evaluation.md); passing known-gap tests do not mean
full restoration. No production adapter or candidate selection is implemented.

## Guest-native ext4 qualification

The opt-in ext4 route reuses the running VM and its existing fingerprinted
image. It refuses missing tools or images rather than downloading dependencies.
The privileged guest helper creates a fully preallocated image outside the
application container. Formatting uses `nodiscard` and completed inode/journal
initialization; reservation is checked again after formatting. Only one fixture
can be active at a time. The allocation and byte profiles use 256 MiB / 8,192
requested inodes; the inode profile uses 64 MiB / 128 requested inodes. Actual
inode totals, filesystem features, UUID, loop identity and mount options are
observed, not inferred from those requests.

```sh
python3 dev/virtualization/ha_ops_dev.py run --input . --ext4 --ext4-profile allocation --recreate --report dev/virtualization/.local/ext4-allocation-report.json -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_ext4.py
python3 dev/virtualization/ha_ops_dev.py run --input . --ext4 --ext4-profile bytes --report dev/virtualization/.local/ext4-bytes-report.json -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_ext4.py
python3 dev/virtualization/ha_ops_dev.py run --input . --ext4 --ext4-profile inodes --report dev/virtualization/.local/ext4-inodes-report.json -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_native_ext4.py
# Interrupted runs retain an exact run ID in the ignored report.
python3 dev/virtualization/ha_ops_dev.py cleanup --ext4 --run EXACT_RUN_ID
```

Admission checks host reserve plus copied input and backing demand, Docker's
reserve, and the actual guest backing-parent bytes/inodes immediately before
preallocation. The application receives three exact private binds: read-only
input, writable output/report storage, and writable `/workspace/ext4`. It runs
as the host's nonzero numeric UID/GID with `CapDrop=ALL`, no devices or added
capabilities, `no-new-privileges`, a read-only root and no network. Container
inspection verifies that boundary before start; each test independently proves
its mountinfo and NativeDisk ext4 profile. Default pytest temporary directories
and the virtiofs output share are not allocation evidence.

Reports remain outside the full filesystem and survive successful resource
cleanup. Each attempt publishes a schema-1 matrix to its own token-named file;
the wrapper checks exact mandatory rows, mounted profile, measured/forecast
terms, recipe families, real failure boundaries and current attempt/token.
Recreation receives the previously validated matrix as a separate explicit
input and must freshly observe the same sentinel device/inode/hash. Missing,
malformed, partial, extra-row or stale reports refuse completion. A report is marked passed only after every required profile row, exact
container absence, normal unmount, exact loop detachment, backing/run-directory
removal, repeated cleanup and unchanged reusable image/VM identity. A failed
proof retains its partial matrix and first error. Cleanup errors aggregate and
return nonzero; unverified container removal prevents unmount/backing deletion.
The helper journals each acquired resource and cleanup transition in private
root-owned controls. A manifest write/fsync/replace failure retains the first
exception while temporary-unlink and descriptor-close failures are appended as
cleanup diagnostics. Structured helper error evidence survives the process
boundary into the wrapper report; pre-replace failures retain the old manifest
and any unremoved temporary as explicit debt. An outside-directory receipt makes interrupted final
manifest removal resumable. Unknown or substituted identities retain explicit
debt; cleanup never uses broad prune/removal, global loop detachment, lazy/force
unmount or VM reset.

The allocation profile holds real outputs at explicit overlap barriers:
incompressible loose objects/spools; complete selected and unselected indexes
with full lock bytes; generated trees/fanouts; no-checkout worktree admin and
materialization; exact Save commits, deep independent refs/logs and full existing
reflog append; complete old/new operation controls and streamed diffs;
independent recovery copies, encoded metadata and replacement temporary; all
Git object kinds including unreachable objects and retained old packs during
no-reuse/no-delta pack/index/full temporary overlap. These are qualification
fixtures, not deployed executors. One actual snapshot/forecast is retained per
case; repeated phase checks forbid product scanners, Git enumeration and
capacity refresh. Separate observer fsync, deduplicated `st_blocks`, inode and
free-space telemetry measure the held outputs without replacing the forecast.
No reserve or positive tolerance conceals a bound overrun.

The failure profiles reach real kernel byte/inode `ENOSPC`. Byte exhaustion
also reaches payload streaming, worktree materialization and state publication
after a durable artifact. Inode exhaustion reports positive remaining byte
capacity. Existing state, ref/object bytes, sentinels and recovery pins are
preserved. Positive short writes retry to exact bytes; zero progress and scoped
write/fsync/publication `EIO` refuse success. The materialization EIO proof enters `copy_blob` and writes/fsyncs 4,096 exact
selected-file bytes before raising at that held descriptor; unrelated journal
writes pass through. Post-replace directory fsync failure
is reconciled as potentially published state. Addressed owner recovery reports
its actual result: unacknowledged partial entries may remain `debt` with exact
resource/owner/operation identities. That is an expected safe preservation
outcome, not evidence of completed rollback.

Observed local qualification (Linux aarch64, ext4 4,096-byte quantum):

| Held family | Physical peak bytes | Forecast additional peak | New live inodes | Forecast inodes |
| --- | ---: | ---: | ---: | ---: |
| Loose objects and spools | 3,600,384 | 23,113,728 | 367 | 1,709 |
| Complete index/lock and trees | 4,268,032 | 23,113,728 | 525 | 1,709 |
| Worktree/admin/materialization and Save refs/logs | 6,287,360 | 23,113,728 | 759 | 1,709 |
| Existing full reflog append/ref atomic overlap | 159,744 | 864,256 | 5 | 25 |
| Complete operation controls/temp/diff/pending envelope | 286,720 | 765,952 | 5 | 23 |
| Two independent copies/metadata/replacement | 3,153,920 | 6,385,664 | 6 | 11 |
| Retained old pack/new pack/index/full temporary | 2,117,632 | 4,296,704 | 4 | 7 |

Each cumulative scope row uses the same immutable Save plan; later rows keep
earlier outputs live. Family fixtures and free-space observations include
existing parent growth and filesystem metadata. Byte exhaustion returned
`errno=28` after 242,614,272 acknowledged filler bytes. The low-inode fixture
observed 128 total inodes and 63 new filler files before `errno=28`, with
60,198,912 bytes still available. Allocation recreation retained filesystem
UUID and sentinel device/inode/hash; all profiles verified exact cleanup and
unchanged VM/image identity. Raw reports under ignored `.local/` contain the
complete plan terms, reserves, versions, observations and owner debt.

This qualifies current dormant recipe demand and existing owner refusal/local
recovery. Future Apply crash recovery, exact metadata restoration, per-path
rollback, remote publication and restart/health integration still need their
own executor proofs. A host-only skip, green generic journal flag or disposable
copy cannot close those obligations.
