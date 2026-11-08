# Dormant migration content review

Migration review remains diagnostic and dormant: no saved decision, browser
session, reload, backend restart, or App update authorizes publication or activation.
A fresh explicit review is required after lifecycle reset. Reset clears selection,
challenge capabilities, dismissal, heartbeat and in-flight responses. Best-effort
unload release captures the exact old view ID and authenticated endpoint; a late
close can never close its replacement. State reset closes only its context and
schedules owned cleanup. Restart and App update restore no ephemeral grants.

Each authenticated context has at most one active content view. Views have a
300-second monotonic lease, renewed by exact authenticated page reads and a
30-second browser heartbeat. At the expiry boundary a view cannot revive; a
fresh explicit review/open is required. Hidden, frozen or offline pages may lose
the lease. One weak daemon timer per Review sweeps every 30 seconds while views
or retryable cleanup debt remain. Shutdown cancels it, invalidates grants and closes all
owned descriptors; a finalizer also closes descriptors when an owner is collected.
A successful terminal decision stops its view heartbeat, clears the viewer and
best-effort releases its exact content, including dismissal with an open viewer.
Lost release converges through expiry without affecting a healthy other context.
Scheduler setup/start failure closes owned descriptors and invalidates grants.
Normal idle retention is at most lease plus one tick, subject to process scheduling
and I/O. Descriptor release precedes disk cleanup and survives cleanup failures.

Before review/open allocation admission, a cleanup-only existing admission lock
retires provable inactive completed receipts through their existing private
quarantine. Active other contexts remain protected and charged. Ordinary admission
then takes a fresh snapshot; no presumed reclamation is subtracted. Corrupt,
foreign, substituted and unowned evidence stays charged with an explicit cleanup
diagnostic. Cleanup still verifies active receipts, locations and descriptor/path
identity, but leaves their full payload hash to the strict explicit request.
Inactive payloads retain both preflight and postcapture content verification.
Known admission/store lock contention and transient I/O errors retry automatically.
Corrupt or unowned evidence and permanent filesystem errors retain diagnostics
without automatic cleanup retries; healthy view leases still expire normally.
A fresh explicit review or content-open retries cleanup after external repair.
Permanent I/O or unprovable ownership can retain bytes indefinitely.
Empty private quarantines and inert pending fragments keep their existing policy.
The allocation recipe retains eight new entries and conservative control overhead.

An explicit current candidate/scope/model review with only obsolete cached-content
proof failure issues a restricted in-memory challenge: refuse/dismiss only, no
content access or approval. The UI states the cache problem and visibly disables
approval and inspection. Safe decisions still require current exact identity,
authenticated context, token, ordinary write admission and previous-decision CAS.
They never trust, repair, adopt or delete corrupt cache evidence. Approval and
content-open retain strict completed proof. A restricted challenge cannot upgrade;
fresh strict review is required. Saved decisions remain inert diagnostics.

Full content uses bounded Git stdout streaming with an opt-in inactivity timeout.
Only accepted stdout resets that deadline; metadata operations retain their total
deadline. Excess producer bytes are rejected before writing them. Caught pending
copy/write/fsync failures capture and reclaim only the held owned inode in the
private quarantine. Abrupt death can leave an inert `.pending-*` diagnostic;
new explicit review may proceed, but the fragment is neither adopted nor deleted.
Promotion/receipt uncertainty and foreign identities remain fail-closed.

Raw byte pages remain exact 16 KiB base64 windows. Text is a separate strict
UTF-8 window with at most three neighboring bytes on either side, aligned to
whole characters independently for direct and reverse navigation. Its labeled
byte range can overlap adjacent pages. BOM codepoints are preserved, malformed
or incomplete EOF sequences explicitly have no text, and exact hex/base64 remain
available. No replacement decoding hides invalid content.
