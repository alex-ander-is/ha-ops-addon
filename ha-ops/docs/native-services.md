# Dormant Core and App service lifecycle

`app/native_services.py` is an explicitly invoked, injected-adapter primitive.
It has no production importer, Supervisor transport, HTTP route, UI, startup
callback, Apply connection or automatic worker. It neither writes native files
nor activates the native workflow. Creating a `ServiceStore` creates a private
directory; discovery can create its stable lock file but never dispatches service
commands. Refresh, restart and version update retain the journal for diagnosis
and explicit recovery; they never resume it. A future caller must use a dedicated
private `/data/native-services` directory.

## Explicit API and lifetime

`prepare(adapter)` freezes the complete inventory before any stop. Its returned
`operation`, `owner` and `revision` fence every subsequent `stop`, `restore` and
`reconcile` call. Every mutation returns the current fence. Stale fences fail
before commands. One unresolved lifecycle cannot be replaced. After a fully
restored result, `prepare(adapter, replace=(operation, owner, revision))` explicitly
replaces the terminal diagnostic record; discovery alone retains it.

The synchronous worker owns no ingress, broker or request object. An explicit
caller may run it independently of ingress. Fixture subprocess checks establish
that architectural independence, not live Supervisor survival. An interrupted
process must be reopened and explicitly reconciled/restored.

All adapter methods receive a finite positive `timeout` from one monotonic
deadline, including time spent inside earlier calls. Readiness and transitional
inventory observations also have a finite poll count. Polling retains only its
latest bounded observation, health and fixed diagnostic, never a growing history.
The adapter **must honor the supplied timeout**: this Python primitive cannot
forcibly interrupt an arbitrary blocking callback. Production transport,
capability qualification and readiness implementation remain future work.

## Injected adapter contract

All objects have exact fields; extra fields, wrong identities and raw Supervisor
envelopes are rejected. Exceptions become fixed diagnostics; exception messages
and credentials are not stored. These are normalized injected types, not upstream
API payloads.

| Method | Response |
| --- | --- |
| `inventory(timeout=...)` | `{complete: true, self: "app:slug", entries: [...]}` |
| inventory entry | `{id, startup, state, manageable, capability}` |
| `observe(target, timeout=...)` | `{target, state}` |
| `command(pending, timeout=...)` | `{operation, owner, attempt, target, kind, outcome}` |
| `evidence(pending, timeout=...)` | Same binding fields, with evidence outcome |
| `readiness(target, timeout=...)` | `{target, status, recovery_mode}` |

Identities are `core` or `app:<stable Supervisor slug>`; explicit HA Ops self
identity must be present. Inventory contains Core and every installed App once.
States are `running`, `stopped` or normalized `unknown`; unknown original state
requires bounded fresh observations and blocks preparation if unresolved.
Capability is `manageable` or `cannot_manage`, matching boolean `manageable`.
The adapter must qualify actual stop/start ability. Protection, `system_managed`,
boot settings and `startup: once` alone do not establish inability to manage an App.

Every originally running manageable App except HA Ops is selected. Every original
state and exclusion reason is frozen, including stopped auto-boot Apps and
capability-excluded Apps. Core's original stopped state is preserved too.
There is no arbitrary App-count ceiling; storage scales with installed Apps,
not polling duration. Stable identities are limited to 255 characters.

Command outcome is `accepted` or `rejected`. The adapter may return `rejected`
only when it guarantees no effect. Transport errors, uncertain responses and
lost acknowledgments must raise instead. Ownership requires accepted command
evidence plus independent matching process observation; receipts retain the
bound attempt and exact typed before/after observations. Command acceptance alone
never proves stopped, started or healthy.

## Persistence, ambiguity and recovery

The own-UID private directory/journal require modes `0700`/`0600`. No-follow opens,
exact fields, duplicate-key refusal and cross-field validation reject corruption
without rebuilding defaults. A nonblocking directory flock plus stable lock-file
flock excludes cooperating workers through commands and probes. Directory, lock
and journal-pointer identities are checked at write boundaries and before dispatch.
Replacing the lock name cannot bypass the directory fence. This is not a security
boundary against a hostile same-UID process replacing the entire private namespace.

Construction qualifies every ancestor namespace link with its parent directory
fsync before the store can dispatch commands. Reopen repeats that qualification,
including directories left visible by an interrupted/failed creation. A namespace
durability failure prevents service effects. A terminal record cannot omit owned
stop receipts while retaining unexplained stopped observations; explicit failed-
stop diagnostics remain valid for partial-stop restoration. Invalidation flag,
diagnostic and receipt/debt relationships must agree in both directions.
Each service persists an independent obligation state: `untouched` →
`stop_pending` → `owned` → `start_pending` → `started`, with an `invalidated`
branch after witnessed external restart. Intent and pending obligation are
persisted atomically **before dispatch**; acknowledgment atomically replaces the
intent with the corresponding owned/start receipt. Exactly one global or archived
attempt must exist if and only if that service has a pending obligation, matching
its stop/start kind. Invalidated services cannot have either kind of attempt.
Qualified non-dispatch evidence or rejection resets only the correct prior state
(`untouched` for stop, `owned` for start). Unknown stop attempts never become owned.
Observation/health errors never discharge an obligation. Every load requires all
receipt, attempt, obligation and invalidation fields to agree, so deleting either
a receipt or ambiguous attempt cannot erase debt even with unknown observations.
`started` records proven process start; independent readiness is still required.

Before each command, intent is file-fsynced, atomically replaced and directory-
fsynced. Dispatch entry, returned acceptance, independent observation and
acknowledgment are separate durable boundaries. Failure preserves the last durable
state and recovery debt. A crash before dispatch can still be ambiguous: persisted
intent alone is not proof that the command was never sent.

There is one active dispatch and at most one unresolved attempt per service.
Archiving ambiguity as bounded per-service debt permits explicit restoration of
independent owned stops without discarding ambiguity or resending that command.
No attempt/error history grows on retries.

`reconcile` handles one unresolved attempt per explicit invocation. A durably
accepted result plus fresh matching observation completes ordinary acknowledgment.
Otherwise injected operation-specific evidence must say:

- `completed`: acknowledge only with independently observed matching state;
- `not_sent`: clear only with fresh observation equal to the before state;
  a later explicit worker call may create a new intent;
- `unknown`: retain recovery-required debt, target, attempt and observation.

Bare stopped/running state, identical state history and third-party state changes
cannot prove lost-ack ownership. Fixture evidence is **not a Supervisor receipt
or idempotency API**. Unknown stops are never restored as if HA Ops owned them.

## Dependency order and health

Stop order is application/once Apps, Core, services, system, initialize. Restore
order is initialize, system, services, originally running Core, application/once.
`once` is deliberately classified with application; same-phase ties use stable
identity order. A failure blocks dependent later phases while independent peers
in the same phase continue. Partial stop failure never exposes `stopped`; known
owned stops remain explicitly restorable. Only originally running selected
services with an owned stop may receive start. Proven starts are not repeated.
A fresh observed external restart invalidates the earlier stop authority; a later
unrelated stopped observation cannot regain it. Independent recovery continues,
while that service remains explicit recovery debt.
Transient unknown/unavailable stop observations can recover on an explicit stop
replay: fresh stopped evidence clears their diagnostic without another command.
This cannot clear the permanent invalidation caused by witnessed external restart.

Readiness is independent of process observation: `healthy`, `unhealthy` or
`unknown`. Core additionally requires known `recovery_mode: false`; true is
unhealthy, missing/unknown is uncertain. Running alone is insufficient. Failed
or malformed probes do not reuse earlier healthy evidence. Healthy restoration
requires every owned stop to have proven start, fresh running observation and
independent readiness. Unknown ownership/failed starts keep `recovery_required`;
unresolved health yields `uncertain` or `unhealthy`. After partial stop failure,
`restored` means all operation-owned stops were restored and checked; it does
not claim every originally selected stop succeeded.

Semantics were checked against maintained Supervisor
[App API](https://github.com/home-assistant/supervisor/blob/main/supervisor/api/apps.py),
[App lifecycle](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/app.py),
[Core API](https://github.com/home-assistant/supervisor/blob/main/supervisor/api/homeassistant.py)
and [ordering](https://github.com/home-assistant/supervisor/blob/main/supervisor/core.py).
App startup can complete with unhealthy/no-healthcheck state. Core info does not
itself provide process/recovery readiness. These facts explain the injected
contracts; no upstream code is copied or wrapped into a live adapter here.

## MQTT availability

The App declares `mqtt:want`, per the official
[service configuration contract](https://contributors.home-assistant.io/apps/reference/configuration/).
Unrelated workflows and context construction do not query the broker. Retained
discovery/deletion request service connection details at each invocation and
reject unavailable, unsuccessful or unusable responses before mosquitto runs.
Supported direct/enveloped success responses and credential aliases remain.
Validation failures use a fixed availability message, never response credentials.
Client disconnects remain invocation failures.
