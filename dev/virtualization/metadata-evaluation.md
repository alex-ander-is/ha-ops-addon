# Native-tree metadata candidate evaluation (step 6.7)

The accepted MVP decision (2026-10-10) is to choose Etckeeper and exclude
Metastore from further consideration because of its fundamental restore bug:
an owner-only change can clear setuid/setgid, changing stored mode 06750 to
0750 while reporting success. Etckeeper restores ownership, full permission
bits and required parent-directory metadata in the executed fixtures.
ACL/xattr support and extensions to Etckeeper for them are outside the MVP
scope; the current HA setup does not use them. The broader comparison below
remains historical evidence, not an MVP requirement. This evaluation does not
implement step 6.8 or change App code.

## Executed environment and package applicability

The repository runner built the development-only image from the existing
digest-pinned Python 3.12.10 bookworm base. Observations are from Linux
`6.8.0-117-generic`, `aarch64`, root, effective capabilities
`00000000a80425fb`, and `/tmp` tmpfs mounted `nosuid,nodev,noexec`.
The prerequisite fixture independently exercised chown, full chmod bits,
named access ACL, directory default ACL, and binary user xattr readback.
The output bind was not used for metadata evidence.

| Component | Executed Debian version | Installation boundary |
| --- | --- | --- |
| etckeeper | 1.18.20-1 / executable 1.18.20 | Downloaded and extracted into `/opt/metadata-tools`; no maintainer scripts or `/etc` initialization |
| metastore | 1.1.2-4+b1 / executable 1.1.2 | Development image only |
| acl / libacl1 | 2.3.1-3 | Development image only |
| libbsd0 | 0.11.7-2 | Metastore runtime dependency, development image only |

Existing image libc6 supplies the other Metastore runtime dependency. No compiler,
build-essential, host package installation or attr executable was added; Python
`os.listxattr/getxattr/setxattr` performs independent binary xattr inspection.
APT fails unavailable exact pins instead of substituting a newer candidate.
Only arm64 runtime execution is qualified here; amd64 package availability is
not amd64 execution evidence.

Exact extracted etckeeper SHA-256 inventory:

| Package file | SHA-256 |
| --- | --- |
| `usr/bin/etckeeper` | `a71a2ae8832c843b372752684866225acf39ea95a44dbb473d050d0f251f8d9a` |
| `etc/etckeeper/pre-commit.d/30store-metadata` | `b781278601de89902da64860c99b63a353d3c2a47da3d22df4b62f19f25e7843` |
| `etc/etckeeper/init.d/20restore-etckeeper` | `cadec5f18f5913d1994aecff37735e16d18245a7c6f19f5212efe48d7755ca20` |

The executed arm64 Metastore binary SHA-256 was
`d8b76f03f15f39a05c8386269530ed469d85c745fe5e70a6db45a4db1a38025f`.

These exact scripts were read inside the image: `30store-metadata` explicitly
does not handle xattrs, scans the whole scope, and stages `.etckeeper`;
`20restore-etckeeper` sources the repository shell script. Hooks remain on the
read-only image; disposable configuration links to those hooks because `/tmp`
is noexec. Only self-generated owned fixture manifests are executed.

## Observed comparison

Readback uses `os.lstat`, `stat.S_IMODE` (all 07777 bits), numerical canonical
`getfacl` entries, exact xattr name/value byte maps, and SHA-256 of regular-file
bytes. It never treats a tool exit code, its dump, or printable xattrs as proof.
The historical file uses daemon ownership and restrictive 0640 permissions.
Both required parents have daemon ownership, 03750 permissions, named access
ACL, restrictive masks, default ACL and a binary user xattr containing NUL and
non-UTF8 bytes. The independent sibling has different metadata. Baselines are
read after all ACL writes because ACL masks can change mode bits.

| Proof | etckeeper | Metastore |
| --- | --- | --- |
| Local Git revert after deleting file and both required parents | **Executed basic pass**: bytes, UID/GID, full modes and parents; **executed gap**: ACL/xattr omitted | **Executed exact fixture pass**, including parent access/default ACL and complete supported fixture xattrs |
| Independent deletion clone fetches restore from local bare remote | Same basic pass and ACL/xattr gap | Same exact fixture pass |
| Fresh clone of restoring revision | Same basic pass and ACL/xattr gap | Same exact fixture pass |
| Changed same-name binary xattr, added stale xattr, changed ACL/defaults/modes | Omission established by the restore cases | **Executed exact pass**: old values restored, stale name removed, ACL/mode coupling correct |
| Missing regular file without Git materialization | Git supplies content in the restore route | **Executed gap**: apply leaves bytes absent, exit 0 |
| Owner differs, mode already equals stored 06750 | **Executed exact pass**: daemon owner/group and 06750 restored | **Executed gap**: daemon owner/group restored, mode becomes 0750, exit 0 |
| Unknown UID/GID (absent from NSS) | Exact unknown-identity behavior not qualified | **Executed gap**: capture omits path with NSS diagnostic, exit 0; apply does not recover owner |
| Nonprivileged apply to root-owned changed file | Not qualified | **Executed gap**: ownership, ACL and xattr remain wrong, exit 0 and empty stderr |
| Malformed signature | Untrusted script restoration prohibited | **Executed rejection with weak signaling**: file/outside sentinel unchanged, diagnostic, exit 0 |
| Regular-file record applied to replacement directory | Not qualified | **Executed rejection with weak signaling**: directory/outside sentinel unchanged, stdout type warning, exit 0 |
| A selected, B changed but unselected | **Executed coupling**: shared script includes B metadata | **Executed coupling**: shared binary includes B metadata |
| Same-path branch divergence | **Executed text conflict**, unmerged stages 1/2/3 | **Executed binary conflict**, unmerged stages 1/2/3 |
| Different-path branch edits | **Executed clean text merge** in bounded fixture | **Executed binary conflict** despite different paths |
| Apostrophe and space in regular filename | **Executed roundtrip**, exact package quote handling | **Executed roundtrip** |

Passing tests for known gaps assert the observed limitation explicitly; they are
not full-restore success or unconditional xfails. The ordinary positive fixture
does not erase the separately demonstrated special-bit defect. The filesystem's
nosuid/noexec policy means executable behavior is not exercised; stored mode bits
are read directly. mtime is an optional Metastore feature, not qualified by this
suite; ctime and inode identity are not restoration guarantees.

## Git transport and selection contract

Content and the corresponding manifest are committed together. The fixture
deletes the regular file and both proper parents, captures/commits the deletion,
and asserts absence before reverting. A local bare remote first transports that
deletion to independent destination and publisher clones. The publisher clone
creates the restoring revision solely from Git history. The destination fetches
it, materializes Git bytes, and then invokes the actual candidate restore route.
A fresh clone repeats restoration without previous inodes. All clones use
`--no-hardlinks`, no alternates, relative metadata paths, and no recovery sidecar.
An in-memory expected snapshot is only an assertion oracle; tools cannot read it.

This proves independent-tree Git transport and relocation under the same kernel
and NSS. It does not prove arbitrary cross-host portability. Metastore owners
are names resolved by destination NSS; the same name can map to a different UID.
POSIX ACL xattrs retain raw numeric principals. Different NSS, ext4, live HA,
SELinux, security/capability namespaces and unsupported filesystems remain
unverified. The measured privilege-denied ACL/xattr case is not a measurement
of a filesystem returning EOPNOTSUPP. Missing support for required MVP metadata
must fail a future adapter's capability gate; no silent downgrade is acceptable.
ACL/xattr capability is not an MVP requirement.

Whole-tree capture cannot represent independent per-path selection by staging A
plus the fresh shared manifest: the test materializes the isolated candidate
index and confirms B's bytes are baseline while B's unreviewed mode leaks.
Raw etckeeper's actual staging effect is redirected into a disposable Git index;
the separately staged canonical sentinel/index stays byte-for-byte unchanged.
The test-only proposal reconstructs a separate baseline tree, applies historical
metadata, changes selected A, retains B and required parents, and recaptures.
That basic-attribute proof is experimental, not a production record parser,
security boundary, full ACL/xattr selection proof or native adapter. Production
selection still needs a canonical per-path representation and safe reconstruction.

## Accepted MVP decision and planned review behavior

Step 6.8 will use Etckeeper for a versioned, repository-only metadata adapter
covering owner/group, all permission bits and required parent directories.
Metastore is excluded because the demonstrated owner-change bug breaks exact
permission restoration while reporting success. Its ACL/xattr coverage does
not change that decision. ACL/xattr implementation and Etckeeper extensions
for them are outside the MVP scope and are not planned by this evaluation.
Per-path selection and safe restoration still require integration: unchanged
upstream hooks cannot run in NativeGit, which deliberately disables repository
hooks and live checkout effects. Existing NativeGit/Inventory/OperationStore
do not implement exact metadata restoration.

A future UI should show per-path before/ours/theirs metadata facets beside
content: owner/group and full mode, plus required-parent rows. Content and MVP
metadata need atomic review. Unresolved conflicts and failures in required
MVP metadata, identity or permissions must block completion. No ACL/xattr UI
or implementation is required for the MVP. No UI changes are part of 6.7.

## Reproduce

After dependency/build-network approval, use repository-owned `setup` to build
the pinned development image, retaining the named VM and older images:

```sh
python3 dev/virtualization/ha_ops_dev.py setup
python3 dev/virtualization/ha_ops_dev.py run --input . -- python3 -m pytest -p no:cacheprovider -q -s ha-ops/tests/test_metadata_evaluation.py
```

The suite has 19 Linux executable cases plus three portable negative-outcome
guards, and emits compact `METADATA_EVALUATION`
records including versions, kernel/mount, exact hook/binary hashes and observed
outcomes. Linux missing tools, capabilities or ACL/xattr support are failures.
macOS runs the three portable guards and explicitly skips the 19 Linux cases;
this does not count as Linux proof. Clean merges require Git success, no
unmerged index stages or conflict markers, and independently restored metadata
from both merged branches. Injected fatal refusal, unmerged stages and text
conflict must fail the real positive fixture as well as the portable outcome
guard. Ordinary positive restore/selection comparisons also require successful
commands and independently matched bytes, ownership, full modes and parents.
Each test removes its owned `/tmp` directory even on assertion
failure; the wrapper verifies container/fixture cleanup. Tests never install
packages, use network access, read credentials or touch live HA.

## Primary sources and applicability

- [Etckeeper README](https://etckeeper.branchable.com/README/) explains metadata shell storage and explicit restoration after checkout; exact Debian scripts above supplied executed-version evidence.
- [Etckeeper changelog](https://sources.debian.org/src/etckeeper/1.18.22-2/CHANGELOG) describes the historical move from binary Metastore data to readable metadata; historical rationale is not a merge guarantee.
- [Metastore upstream README](https://github.com/przemoc/metastore), [manual](https://github.com/przemoc/metastore/blob/master/metastore.txt), [format](https://github.com/przemoc/metastore/blob/master/FILEFORMAT), [capture source](https://github.com/przemoc/metastore/blob/master/src/metaentry.c) and [apply source](https://github.com/przemoc/metastore/blob/master/src/metastore.c) establish API, names/raw xattrs, binary storage and source-backed limitations. Upstream master is not claimed identical to the Debian binary; the fixture independently measures the pinned package.
- [Linux chown](https://man7.org/linux/man-pages/man2/chown.2.html) explains clearing executable special bits; [POSIX ACL semantics](https://man7.org/linux/man-pages/man5/acl.5.html) explain mode/mask coupling and access/default ACL.
- Debian bookworm package pages for [etckeeper](https://packages.debian.org/bookworm/etckeeper), [metastore](https://packages.debian.org/bookworm/metastore) and [acl](https://packages.debian.org/bookworm/acl) provided package candidates; exact pinned build and runtime inventory are the execution evidence.
