# Original App runtime qualification

Native remains dormant. This work adds no production import, UI, Apply path,
migration, automatic recovery, or activation. The qualification target is the
installable `ha-ops/` Docker context, with its original `/app`, Git, Python,
OpenSSH, HTTPS helper and linked libraries.

## Reproducible package profile

`build.yaml` pins the effective Supervisor base for each declared maintained
architecture. The Dockerfile's architecture-neutral default is the immutable
HA base `3.24-2026.08.0` OCI index
`sha256:93ef607824e3f27e868f11b10938283a98bf880ed57bcf8eaa81c6c2d521f6f5`.
The per-platform digests are recorded in `build.yaml`.

`runtime-apk-aarch64.lock` and `runtime-apk-amd64.lock` contain the entire
resolved installation closure: 56 exact archive URLs and SHA256 hashes per
architecture, including dependencies that upgrade packages inherited from the
immutable base. Installation downloads only these archives, checks every hash,
then uses APK's trusted signature verification with `--no-network`. There is no
version-range or current-index fallback. Inherited packages remain fixed by the
base digest. `runtime-pip.lock` pins both architecture-specific CPython 3.14
musllinux annotatedyaml artifacts by URL/hash and rfc8785 by version/hash.
PyYAML comes from the APK closure; pip dependency resolution is disabled.

The resolved candidate uses Git `2.54.0-r0`, Python `3.14.8-r0`, OpenSSH
`10.3_p1-r1`, musl `1.2.6-r2`, OpenSSL libraries `3.5.9-r0`, curl
`8.22.0-r0`, and zlib `1.3.2-r1`. Full package and executable/library identities
are in the canonical `runtime-profile.json`.

Alpine/PyPI/HA wheel retention is outside this repository's control. The local
runner retains checksum-verifiable archives in `.local/runtime-cache` for
evidence; the normal install does not silently use that cache or promise old
URLs indefinitely. If an exact artifact disappears, the build stops with an
explicit profile-resolution/requalification diagnosis. Do not replace a pin
with `latest`, remove a checksum, or disable APK signatures.

## Admission and architecture

`native_runtime.qualify()` observes the exact OS, architecture, installed APK
list, Git/Python/SSH/TLS versions, real executable/helper paths and binary/library
hashes. It checks the committed profile before network work or scratch
allocation. A missing, malformed or changed tuple fails actionably. Environment
variables cannot add a supported profile. The old Debian Git 2.39.5/Python
3.12.10 development profile remains separate. Native SSH uses absolute
`/usr/bin/ssh`; inherited PATH cannot substitute another SSH client.

| Architecture | Package availability | Executed qualification |
| --- | --- | --- |
| aarch64 | Exact base, signed 56-APK closure and wheel hashes resolved | Local native aarch64 Colima VM; evidence below |
| amd64 | Exact base, signed 56-APK closure and wheel hashes resolved without executing foreign code | Unqualified; no amd64/emulation success claimed |
| armhf / armv7 / i386 | Removed from config with explicit human approval; no maintained base/lock supplied | Unsupported |

The human approved retaining aarch64 and amd64 only. Config declarations, build
bases, APK locks and pip architecture selectors agree; this does not qualify
amd64. The user's HA hardware is ODROID-N2/aarch64. Local VM proof is not an actual
Home Assistant installation, HA kernel, storage-driver or Supervisor proof.
Step 24 must require successful complete runtime proof for each architecture it
enables; a config declaration, build, version string or another architecture's
result never suffices.

## Isolated original-image proof

The repository runner verifies the named local Colima endpoint and resource
headroom. It snapshots the installable context while preserving source directory
and executable modes. The App's root filesystem is read-only; only tests and
hash-pinned pure Python pytest dependencies are mounted outside `/app`. The
runner compares all original App file hashes with the build-source manifest.
No client binary, helper, library, Python stdlib or production module is mounted
over the image.

Each run creates a fresh owned extraction from the seven hash-reviewed pure
Python pytest/xdist/execnet dependency wheels. Its exact file hashes and directory
closure are checked before execution and cleanup; extra files, changed content,
symlinks and hardlinks refuse. Unknown old extractions remain untouched and are
never mounted. The genuine-wheel stale `ssl.py` regression verifies that old
cache content cannot replace the original App stdlib. Tests cannot resolve pip
dependencies during execution.

`runtime-required-cases.json` is the reviewed, versioned required-node manifest,
whose hash is pinned in `runtime-toolchain.json`. Named complete and focused
groups have separate explicit selectors and identities. Collection must match
that predeclared set; JUnit must contain every identity exactly once with
consistent suite counts. Missing, extra, duplicate or contradictory outcomes
fail. Collection drift never rewrites or approves the manifest: review the
manifest diff and hash deliberately when adding or removing cases.

An exact-owned, inspected 1-GiB tmpfs volume holds disposable fixtures. A separate
pinned Debian receiver image supplies only SSH/TLS server tooling. It joins the
original App's inspected `network=none` namespace, with no host network, ports,
Docker socket, proxies or production credentials. Loopback RPC acknowledges
control updates synchronously in both directions. Root SSH and nonroot HTTPS
fixtures remain distinct; ext4 uses existing nobody UID/GID 65534 in both App
and receiver, without a passwd overlay or `safe.directory=*` bypass.

The ext4 fixture retains the existing device/mount/noexec/nonroot and complete
matrix validation. The original App is removed before the recreation phase;
only the exact owned namespace, volume and retained fixture state survive.
Lost-ack/readback-loss publication is reopened in a fresh App container, remains
inactive until explicit recovery, and reconciles the identical C without a
second push. Cleanup failures fail the proof; only recorded containers,
tmpfs volume and ext4 backing/loop/mount are removed. VM and reusable images stay.

Git 2.54 adds one empty per-worktree `refs/` directory. Its
[files backend](https://raw.githubusercontent.com/git/git/v2.54.0/refs/files-backend.c)
creates this unconditionally. The adapter admits only this empty directory;
children, a regular file or symlink refuse. Its identity is journalled, and the
original frozen NativeDisk forecast now reserves one additional directory block
and inode plus its journal entry. Real ext4 overlap proof exercises the change.

Initial harness attempts correctly failed on virtiofs allocation admission and
cross-share Unix sockets (58 failed/42 passed), then on private snapshot directory
modes. These were harness defects, not qualification. The corrected runner uses
Linux tmpfs/loopback RPC and preserves original source modes; no product
filesystem guard was relaxed.

## Requalification commands

Run from the repository root. These commands only use the named local test VM;
network downloads occur during explicit resolution/build, never during tests.

```sh
python3 dev/virtualization/runtime_qualification.py resolve --arch aarch64
python3 dev/virtualization/runtime_qualification.py resolve --arch amd64
python3 dev/virtualization/runtime_qualification.py resolve-pip
python3 dev/virtualization/runtime_qualification.py build
python3 dev/virtualization/runtime_qualification.py probe
python3 dev/virtualization/runtime_qualification.py run --group network --report ${TMPDIR:-/tmp}/runtime-network.json test_native_runtime.py test_native_network_publication.py
python3 dev/virtualization/runtime_qualification.py run --group native-git --workers 4 --report ${TMPDIR:-/tmp}/runtime-git.json test_native_git.py
python3 dev/virtualization/runtime_qualification.py run --group native-save --workers 2 --report ${TMPDIR:-/tmp}/runtime-save.json test_native_save.py
python3 dev/virtualization/runtime_qualification.py run --group native-scope --workers 2 --report ${TMPDIR:-/tmp}/runtime-scope.json test_native_scope.py
python3 dev/virtualization/runtime_qualification.py run --group keygen --report ${TMPDIR:-/tmp}/runtime-keygen.json test_native_network_publication.py::test_original_app_generate_deploy_key_and_legacy_ssh_authentication
python3 dev/virtualization/runtime_qualification.py run --group recreation --recreate --report ${TMPDIR:-/tmp}/runtime-recreation.json test_native_network_publication.py::test_original_app_container_recreation_retains_pending_same_commit
python3 dev/virtualization/runtime_qualification.py run --group ext4 --ext4-profile allocation --recreate --report ${TMPDIR:-/tmp}/runtime-allocation.json test_native_ext4.py
python3 dev/virtualization/runtime_qualification.py run --group ext4 --ext4-profile bytes --recreate --report ${TMPDIR:-/tmp}/runtime-bytes.json test_native_ext4.py
python3 dev/virtualization/runtime_qualification.py run --group ext4 --ext4-profile inodes --recreate --report ${TMPDIR:-/tmp}/runtime-inodes.json test_native_ext4.py
```

For an upgrade, first review and update the candidate base digests in the
Dockerfile, `build.yaml` and resolver, and the exact Git pin in the resolver's
`PACKAGES` constant. Resolving intentionally changes lock inputs. Review signed-source provenance,
exact closure/hash changes and the observed immutable image before editing the
admitted tuple; `probe` never self-approves its output. Rebuild after a tuple
change, then execute the complete commands above and the repository-wide gate.
Changes to base, Git, Python, SSH, TLS, musl, curl, zlib, helpers or relevant flags
require affected transport/output/authentication/allocation/lifecycle proof
before admission. Never reuse a report for an unexplained image/source change.

Reports record the immutable image and source-file manifest, container/mount/
namespace/volume inspection, complete JUnit IDs, pass/fail/skip accounting and
cleanup. A required skipped test fails qualification. The single SSH
accepted-readback-loss scope combination is explicitly covered through HTTPS;
ext4 reports label the other bounded profiles and recreation-only matrix skips.
The scope SSH accepted-readback-loss combination itself is unexecuted: scope
graph-loss orchestration is exercised through HTTPS, and real SSH lost-ack
same-C recovery is independently exercised by the network matrix. Reports retain
that exact excluded combination; it never counts as passed. No skipped
platform/kernel/network/ext4 obligation counts as executed proof.

## Executed local evidence, 2026-10-17

The rebuilt two-architecture config image is
`sha256:85eb2d7ba8a5ba5ec56a5a9379fd1f28f6f40fa046448fe4f782fc024687309b`.
Its 39 original App files have canonical manifest SHA256
`1cf5ee43e38377d9595fd06020976551e372edcfad24f92e2eaf8b8cbd0f2306`.
Full raw JSON/JUnit, logs, image/source manifests and exact report hashes are
retained under `.local/runtime-evidence/2026-10-17-aarch64-r1`; committed proof
metadata is in `dev/virtualization/runtime-toolchain.json`.

| Proof | Executed result |
| --- | --- |
| Complete NativeGit, including process limits and fixed Git 2.54 worktree grammar | 577 passed, four isolated pytest workers, 338 seconds |
| Complete Native Save | 127 passed, two workers, 65 seconds |
| Complete Native scope | 69 passed, two workers, 147 seconds; exact SSH graph-loss exclusion above |
| Original App profile and authenticated network matrix | 102 passed; no skips |
| Production deploy-key generation and SSH authentication | Focused case passed; original App ssh-keygen, private/public modes, parser/newline and temporary-file cleanup checked |
| Lost-ack/readback-loss, actual App removal and explicit same-C recovery | First App 1 passed; replacement App 1 passed |
| Nonroot ext4 allocation and physical overlap | 9 passed; replacement 1 passed |
| Nonroot ext4 byte ENOSPC, including after network admission | 3 passed; replacement 1 passed |
| Nonroot ext4 inode exhaustion | 2 passed; replacement 1 passed |
| Installer checksum drift and missing exact archive | Both refused before APK installation; unchanged installer evidence retained from the initial period |
| Portable runtime, wrapper and release-readiness guards | 355 passed |

Every new runtime proof used the same closed test-dependency manifest SHA256
`a23069a760fc74a3cd7d9bc137a470d9c4fe479b4eb19bbca9b4727d5e6ffdc1`
and the reviewed required-case matrix. All exact identity/count checks passed;
cleanup reports are empty. Ext4 other-profile cases execute in their separate
bounded profile, and recreation-only skips retain and revalidate its prior
complete matrix. The scope SSH graph-loss combination remains explicitly
unexecuted and never counts as passed.

NativeGit and Save ran the preceding image
`sha256:9c3f180f610f2e3bc558f8796aa363768f88e4e91562f6b4fa22410cf2e71f14`.
The rebuild changes only the config architecture list; its entire original App
manifest and observed runtime tuple are exactly equal. Scope, network, keygen,
ext4 and replacement proofs ran the rebuilt image. Reports retain their actual
image IDs rather than relabelling an earlier execution.

The initial serial core command timed out after 1,200 seconds and is not proof.
The completed split runs preserve every reviewed identity. NativeGit's slowest
cases repeatedly create or interrupt real Git worktrees, with many fault-path
calls around five seconds; bounded independent workers complete this matrix
without changing App clients, libraries or production code. A first split scope
run found that its memory-measurement child lost `/testdeps`; the fixture now
retains the same closed test dependencies, and the complete scope rerun passed.
Historical network/ext4 results used an extraction whose closure was not
attested; those results remain preserved and are superseded by the fresh runs.
Independent sealed-source review and the repository-wide gate remain required.
