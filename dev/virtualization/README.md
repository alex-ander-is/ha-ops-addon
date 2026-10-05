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
