# Dormant migration content review

Migration review remains diagnostic and dormant: no saved decision, browser
session, reload, backend restart, or App update authorizes publication or activation.
A fresh explicit review is required after lifecycle reset. Browser reset clears
content selection and in-flight responses; server forget closes only that
context's active descriptor and drops its challenge. Completed receipts remain
inert evidence and can be retired by a later explicit open with ownership proof.

Each authenticated context has at most one active content view. Opening,
replacing, paging, closing, or forgetting another context preserves other active
views and challenges. Retained views from all contexts and inert pending
fragments remain charged by disk inventory. The admission recipe retains eight
new entries: pending payload (later renamed), private retirement directory,
receipt pending (later renamed), and conservative control/retirement overhead.
Other contexts and previous completed payloads are baseline overlap, never
subtracted before a replacement completes.

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
