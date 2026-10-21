# Dormant storage header and local Compact

`NativeStorage` is available only to an explicitly qualified isolated adapter.
The ordinary server has no storage route or native activation. The reusable
Vaadin storage header renders only for `view.native_storage.qualified === true`.
Legacy disk and Docker actions retain their existing workflow.

Calculate takes one initial NativeDisk snapshot. Its saved diagnostic contains
the last completed timestamp, logical and allocated category totals, opaque
browser authorization overhead, actual destination-volume free bytes/inodes,
and a qualified pending-operation peak or an explicit unknown/absent reason.
Auth filenames, identifiers, contents and digests never enter this view. A
verified absent optional authorization store is identified as absent; unsafe or
unavailable accounting is incomplete. The below-500-MiB warning is advisory.

The private bounded `data/native/storage-status.json` record is atomically
published with file and directory fsync. Later failed/incomplete attempts
preserve the last completed measurement and persist separate attempt diagnostics
when their exact destination/control envelope can be admitted from that same
snapshot. If persistence itself fails, the first fault and secondary cleanup
failures remain visible to the caller; no successful completion is claimed.
After-replace publication faults attempt exact-preimage restoration, never adopt
a foreign replacement, and retain restoration debt if it cannot be proved.
Status, refresh, restart and update read only this exact diagnostic, perform no
scan or maintenance, and cannot reconstruct execution authority.

Compact preview obtains its own single initial snapshot, a canonical owner graph,
the exact obsolete content-addressed artifact set, immutable all-local-object
proofs, and NativeDisk's full no-delta repack demand. Savings separates measured
obsolete-artifact allocated bytes from unknown Git recompression savings. Git
may save little, do nothing, or grow. There is no trial repack and no automatic
Calculate after completion. Cleanup is bounded to 64 exact reviewed artifacts
per explicit operation; other owned and ambiguous files remain protected.

A separate Confirm Compact consumes one instance-issued review before admission,
then holds admission, canonical store and Git-resource locks nonblocking through
maintenance and cleanup. Existing unresolved resources or ownership are protected
and refused; optional missing ownership is unavailable rather than initialized
by preview. Healthy retained recovery pins and their artifacts are preserved.
The concurrency boundary is cooperative HA Ops writers; advisory locks do not
serialize arbitrary external Git processes. Refs, configuration, index, graph,
original objects and exact cleanup targets are checked at the relevant fences.

The exact installed Git command disables hooks, network, automatic maintenance,
bitmaps, MIDX and reverse indexes; uses index version 2, one thread and fsync;
and runs `repack -a -d --keep-unreachable -F --window=0 --depth=0 --threads=1
--no-write-bitmap-index --no-write-midx -n`. It preserves loose and packed
unreachable objects, refs, history, index, authorizations, backups and live files.
Existing ancillary pack formats remain explicitly protected/unsupported.
No gc, prune, reflog expiration, history rewrite or broad temporary-file sweep is
reachable. Only exact single-link regular `.diff`/`.blob` artifacts, unreferenced
by the canonical graph and proved by SHA-256, can be removed. The graph and
retention state are not rewritten.

A Linux subreaper guardian owns Git's private process group and inherited copies
of every held flock description. Timeout, cancellation or caller death closes
its private parent pipe; it kills the group and reaps all adopted descendants
before releasing those locks. A guardian whose quiescence cannot be established
retains locks and produces uncertainty/debt rather than false safe completion.
The durable effect-start record precedes spawning. Interrupted records reopen
as uncertain with no automatic retry, cleanup or authority restoration.

Reviews, confirmations and request generations are volatile. Cancel, refresh,
disconnect, process restart or backend-incarnation/version changes clear the
UI grant and reject stale confirmation. Repeated independent browser lifetimes
do not have a permanent session-count ceiling. Retained measurement diagnostics
never become a Plan, Admission or review. New explicit Calculate or Compact
starts another independent snapshot/authority lifetime.

Before a later Calculate begins acquisition, a fixed-width safe-ASCII attempt
marker updates only its already allocated diagnostic bytes on qualified ext4
or tmpfs. It allocates no new block/inode and does not borrow saved capacity or
wait for an existing disk admission. An interrupted/short marker write remains
valid bounded JSON and reopens as explicit uncertainty while preserving the
previous measurement. An unsupported/COW filesystem refuses this marker; a
first-ever acquisition failure with no admitted diagnostic file explicitly
reports durable diagnostic unavailability rather than fabricating persistence.
Observer reads never update an operation's immutable diagnostic receipt. Exact
original artifact ancestor identities and the canonical held descriptor are
checked again after fault hooks immediately before each unlink.
