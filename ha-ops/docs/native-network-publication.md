# Dormant authenticated network publication

This dormant implementation supplies native-tree step 6.12 network transport.
Independent audit and the full repository gate remain required before delivery;
production activation is outside this contract.

`NetworkGitCLITransport` is an exact supported peer of `NativePublication`,
separate from the historical local bare route. Constructors inspect local
configuration/tool identity only. Construction, diagnostics, lifecycle and
version transitions never connect, fetch, push or settle resources.

## Route and authentication contract

The tagged `native-network-route-v1` endpoint records scheme, direct hostname,
port, repository path, explicit SSH user and qualified destination branch.
It records a route digest rather than a fabricated path/inode identity.
Local intent encoding remains readable. Direct SSH URL and qualified scp
spellings are supported. SSH aliases, proxy commands, URL query/fragment,
escaped or shell-shaped repository paths and HTTP downgrade are refused.
The actual command disables ambient URL rewriting, global/system configuration,
credential helpers, proxies, redirects, reverse indexes, automatic maintenance,
tag following and submodules. HTTPS certificate verification stays enabled.

Every command checks current `repo_url`/`repo_branch` against the detached
credential-free route. Authentication can rotate while that route is unchanged.
Configured manual or generated deploy keys use the existing `git_auth` sources.
Manual key files used by legacy callers preserve a final newline and shell-quote
their identity path. The native adapter creates a sealed anonymous Linux key
descriptor bounded to 65,536 bytes, with at most another key-sized in-memory
copy. Git closes nonstandard inherited descriptors before launching SSH; SSH
therefore reads the exact live owning process's private proc descriptor. The
owner holds it through command/process-group reaping and closes it on every exit.
No disk key file is created for native advertisement or publication.

HTTPS uses configured URL authentication or an explicitly supplied existing
askpass source. URL userinfo is removed from argv, route and durable intent;
the authentication header lives only in the command environment. Only whitelisted
authentication/TLS variables are reintroduced after ambient Git, SSH and proxy
routing controls are stripped. Durable results contain structured reason codes
and transcript hashes, never headers, passwords, key text or raw transport stderr.

## One admission and independent graph

Before any incoming object allocation, publication freezes C, reviewed R,
local expected-old CAS, exact intent/state recipes and an accounting prediction
of the complete hash-verified existing local R ancestry. Prediction supplies
no remote authority. The sole `NativeDisk` snapshot and forecast precede all
transport scratch allocation. A new isolated network object repository must
independently fetch and hash the entire original R graph and produce the exact
predicted digest under an unchanged advertisement before candidate consumption
or owner acceptance. Missing local ancestry refuses before allocation.
An initial transport failure retains an unchanged unconsumed local review only
after the serialized local source and detached authority fields requalify.

The canonical `NativeGit` resource journal owns each scratch directory, private
marker, lease and acknowledged descriptor/hash entry. Each graph uses a fresh
empty bare object repository without alternates, local C, lazy fetch or shallow
configuration. Fetched raw commit bytes are independently rehashed for SHA-1
or SHA-256, then their exact parent edges are walked. A positive may stop at C;
a negative requires complete parent closure. Authentication failure, missing
parent/object, hash mismatch, byte/commit work exhaustion or changing ref gives
uncertainty. Fresh readback is repeated by the proof compiler before local CAS.

The command is pinned Linux Git 2.39.5 `fetch --keep`, fixed protocol v0,
`fetch.unpackLimit=0`, `transfer.unpackLimit=0`, no tags, no FETCH_HEAD and no
maintenance. Fixed protocol v0 excludes protocol-v2 packfile URI downloads.
One fetch-pack/index-pack execution produces a single retained pack/index
family and bounded keep message. Even an unrequested shallow server can create
one `shallow_XXXXXX` temporary file; it is separately charged and cannot prove
complete negative ancestry. A cold repository supplies no thin-pack bases.
Every writer and child inherits a kernel per-file `RLIMIT_FSIZE`; no invented
fetch `--max-input-size` option or post-hoc size check supplies the bound.
The transient pack/index promotion can expose both temporary and final names
for one inode. Their extra directory/name cost is reserved; these are not two
independent packs. Kernel inotify creation events qualify short-lived shallow
names without relying on a sampling interval to observe every allocation.

Before a fetch writer is allowed to execute Git, an isolated Python gate waits
on an anonymous pipe. Its new process-group ID is durably recorded in the exact
scratch journal before the owner releases the gate. The group inherits the
same file cap. Parent death before release gives EOF without Git execution;
parent death after release leaves the recorded orphan group. Explicit cleanup
refuses a live, stopped or unreadable member. A bounded two-pass Linux proc
observation can distinguish terminated zombies, which cannot write or fork,
from active writers; observing a reused ID with a live group still refuses.

The forecast includes three independently capped pack/index/shallow extents,
the keep/control files, directory/name inodes, private marker/lease and the full
prospective canonical resource journal. It subtracts the exact existing
publication/control allocations, current/pending control reserve, observed
growth and allocator overhead from the initial available capacity. The remaining
physical capacity determines the largest equal kernel cap for the three
writers; ext4 additionally charges its existing conservative extent metadata
recipe. There is no fixed repository-size ceiling or fractional free-space
heuristic. Later phases retain the same forecast. Each scratch must retire
durably before its envelope can be reused; a cleanup error stops the flow.

## Publication and explicit recovery

Only exact immutable C is sent to the frozen branch with explicit expected-old
R lease. The durable possible-send marker precedes the command. Exit status
and porcelain never establish acceptance. SSH porcelain removes the explicit
user from its displayed To URL; classification matches that exact pinned
credential-free display. Accepted C, including acceptance with a lost response,
must be independently reachable from a fresh fetched destination graph before
the existing local expected-old CAS and pin-retirement protocol execute.

Explicit recovery retains identical stored C. Lifecycle/diagnostics do not
replay publication. Reconcile reads back without retrying push. Terminal pin
cleanup uses stored result/current owner/local resource receipts and works
without authentication, a CA file or an available remote.
`transport_debts()` exposes canonical scratch ownership even before a native
worker exists. `settle_transport_resource()` is a fresh explicit offline action
for exact already acknowledged owner/operation resources.

SIGKILL after pack/index creation but before descriptor/hash acknowledgement
leaves unknown bytes. Ordinary recovery preserves them. A separate explicit
`review_transport_disposition()` reads the exact current root, original marker,
lease, inode/mode/hash membership and closed hardlink counts. No filename or
valid Git content supplies an ownership claim. A fresh
`approve_transport_disposition()` authorizes discarding precisely that review;
copied, mutated, stale, replayed and foreign-issued reviews refuse. The wrapper
also fences the current lifecycle owner and durable state digest. A reopen or
version transition requires a new review.

The full disposition receipt is durably published before any unlink. This is a
separate local explicit settlement with its own exact journal-overlap capacity
check, rather than another publication admission. Each deletion checks remaining
membership, exact parent descriptor and hashes, then fsyncs and checkpoints
progress. Interrupted unlink, marker removal or root removal resumes only from
that receipt. Foreign reoccupation refuses. Temporary/final links are permitted
only when every link belongs to the reviewed namespace; outside links remain
untouched. Receipt persistence failure before authorization performs no deletion.
Persistent filesystem failures remain truthful addressed debt. After settlement,
the unchanged unconsumed candidate can receive a new publication admission.

Before any resumed cleanup, the stored digest is recomputed from canonical
reviewed entries, command ID, resource/operation/owner identities, root and lease
identities, and the adapter/storage binding. Mutable deletion progress is excluded
so an interrupted authorized cleanup can resume. Inconsistent persisted inputs,
including a changed entry hash with the original digest, refuse before deletion.
This is a corruption check, not authentication against a writer able to replace
both the entire journal and its digest. Earlier unshipped receipts with the
non-recomputable digest refuse; no compatibility fallback authorizes their cleanup.

## Executed proof map and remaining gates

| Guarantee | Executed isolated proof |
| --- | --- |
| Existing SSH key sources | Real manual/generated legacy and anonymous sealed native keys |
| HTTPS authentication | Real TLS smart Git backend with URL and askpass credentials; missing authentication refused |
| Route secrecy/fencing | Portable grammar plus real wrong CA, HTTPS redirect refusal, hostile inherited routing and post-admission route mutation |
| Exact publication and one baseline | Authenticated SSH manual/generated and HTTPS, both object formats; exactly one snapshot/forecast |
| Refusal/races/lost response | Actual receiver hooks, stale expected-old lease and accepted receiver with disconnected SSH/TLS acknowledgement |
| Independent bounded incoming graph | Cold pack-only small/many/8 MiB compressible fixture; oversized incompressible pack interrupted at 262,144 bytes; shallow ancestry remains incomplete |
| Initial failure and addressed debt | Fetch rejection retains unconsumed review/no accepted worker; exact acknowledged scratch cleanup error settles offline |
| Explicit lifecycle recovery | Accepted receiver plus failed readback, diagnostic-only lifecycle, explicit identical-C reconcile without push |
| Offline terminal cleanup | Successful owner result settles after removing credentials/CA |
| Physical forecast | tmpfs peak 69,632 bytes/24 new entries; nonroot ext4 peak 114,688 bytes/23 new entries; both retain their sole frozen forecast |
| Unacknowledged crash leaves | Actual SIGKILL before ACK, byte retention, fresh reviewed disposition and successful unchanged-candidate publication; receipt/unlink/marker/root interruption and reoccupation regressions |
| Orphan writer | Actual parent SIGKILL during fetch; recorded live group blocks disposition, stopped group permits fresh explicit settlement |
| I/O and interruption | Actual kernel ext4 ENOSPC during admitted network fetch; injected scratch short-write/ENOSPC; real send-child timeout retains uncertainty until explicit identical-C retry |
| Graph and CAS | Real receiver descendant over SSH/HTTPS, local CAS conflict and explicit readback; corrupted receiver object cannot establish acceptance and retains pins until repaired explicit reconcile |

Tests are in `test_native_network_publication.py`,
`network_publication_helpers.py`, `test_native_publication.py` and the ext4
matrix. The reusable development image pins Git, OpenSSH and OpenSSL. Loopback
services run inside the existing network-none runner with ephemeral fixture
keys/certificates and are closed/reaped after each test. Nonroot ext4 uses HTTPS;
it does not weaken privileges to run the root SSH fixture.

The complete repository suite, three independent tester passes and final local
delivery remain separate acceptance gates. Tests qualify both object formats
against isolated servers; they do not establish GitHub SHA-256 compatibility.

## Authoritative evidence

The implementation used pinned Git source rather than an assumed fetch flag:
[fetch-pack](https://raw.githubusercontent.com/git/git/v2.39.5/fetch-pack.c),
[fetch-pack command](https://raw.githubusercontent.com/git/git/v2.39.5/builtin/fetch-pack.c),
[index-pack](https://raw.githubusercontent.com/git/git/v2.39.5/builtin/index-pack.c),
[shallow temporary files](https://raw.githubusercontent.com/git/git/v2.39.5/shallow.c),
[HTTPS controls](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/config/http.txt),
[reverse-index controls](https://raw.githubusercontent.com/git/git/v2.39.5/Documentation/config/pack.txt),
[credential sources](https://git-scm.com/docs/gitcredentials) and
[Python resource limits](https://docs.python.org/3/library/resource.html).
The comparable maintained
[go-git remote implementation](https://raw.githubusercontent.com/go-git/go-git/master/remote.go)
supports explicit transport and expected-old comparison but supplies neither
this independent acceptance protocol nor the disk forecast; no code was copied.
Dev-only package pins were checked against
[Debian OpenSSH](https://packages.debian.org/bookworm/openssh-server) and
[Debian OpenSSL](https://packages.debian.org/bookworm/openssl).
GitHub documents supported [remote URL/authentication modes](https://docs.github.com/en/get-started/git-basics/about-remote-repositories)
and [single-repository deploy keys with explicit write access](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys).
No real GitHub connection or publication was used for qualification.
# Original App runtime binding

The network transport now checks the exact
[original App runtime profile](native-runtime.md), separately from Debian
development qualification. Native stays dormant. Git 2.54 qualification must
execute the original App SSH/HTTPS helpers, packed/shallow output, authentication,
limits, independent graph/CAS/recovery and physical allocation matrix. Unknown
tuples refuse before network/allocation; there is no version-override switch.
