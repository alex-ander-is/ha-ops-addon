# Direct browser access

HA Ops listens separately on internal TCP port 8100. Configure its exposed host
port in the App network settings. The ingress page always offers an editable
**Direct address**, **Allow this browser**, **Open in new window**, and **Set up
later**. The suggested LAN address uses port 8100; edit it if your host mapping
differs. Public/cloud ingress addresses require a manually entered LAN address.

From trusted Home Assistant ingress, explicitly allow this browser. A one-time
120-second grant opens the direct page; its fragment is removed before network
requests. The direct listener exchanges it for a distinct remembered-browser
cookie. Cancelled, expired, interrupted or restarted enrollment requires another
explicit allowance from ingress. No action runs during login, refresh or reconnect.

HTTP is supported for this trusted home LAN MVP. The user accepts that someone
able to intercept LAN traffic can impersonate an authorized browser. External
access protection and token revocation are deferred work. Keep the host port
inside the trusted LAN. No DNS, certificate, password, passkey or VPN is required.

For optional HTTPS, set `direct_https: true`, place existing certificate/key files
under `/ssl`, and set `direct_certfile` and `direct_keyfile` to their filenames.
The defaults are `fullchain.pem` and `privkey.pem`. `direct_origin`, when supplied,
is the exact external HTTP(S) origin, including a nondefault host port. Invalid
TLS configuration disables direct access with an ingress diagnostic; it never
silently falls back to HTTP. Ingress remains usable.

Browser authorization uses a host-only HttpOnly SameSite=Strict cookie scoped to
`/direct/`. Its requested lifetime is 400 days and authenticated page visits
renew it. HTTPS additionally sets Secure. The server imposes no idle or absolute
expiry. Browsers may evict/cap stored cookies: clearing browser data, cookie
removal or private browsing requires another allowance. Authorization survives
App restart/update because only per-token IDs, secret hashes, origin and creation
time live under private `/data/direct-auth` (directory 0700, file 0600). This data
is outside Git, managed candidates and ordinary state snapshots. Removing the
private authorization store invalidates all remembered browsers. Corruption or
unsafe permissions fail closed; do not reset these files automatically.

The direct UI exposes only diagnostic native operation, service and recovery
journals from an explicitly qualified local composition. Production currently
has no qualified native live adapter, which is shown as unavailable. Native
Apply and recovery actions remain disabled. Legacy Save/Apply routes are ingress
only; direct authentication grants no operation consent. Refresh/new tabs keep
browser authorization and discard enrollment intent. Restart/update discards
one-time grants and process CSRF values, reacquired without reenrollment.

## Source and proof map

- [Home Assistant ingress](https://contributors.home-assistant.io/apps/guides/ingress/):
  only actual Supervisor TCP peer `172.30.32.2` may issue browser grants.
- [App configuration](https://contributors.home-assistant.io/apps/reference/configuration/):
  host-port mapping, persistent `/data` and read-only `/ssl` mapping.
- [MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie):
  host-only, HttpOnly, SameSite, Secure and Max-Age attributes.
- [MDN Location.hash](https://developer.mozilla.org/en-US/docs/Web/API/Location/hash)
  and [History.replaceState](https://developer.mozilla.org/en-US/docs/Web/API/History/replaceState):
  fragment removal before same-origin exchange.
- [Python SSLContext](https://docs.python.org/3/library/ssl.html#ssl.SSLContext.load_cert_chain):
  existing certificate/key loading without runtime dependencies.
- Maintained comparison: Home Assistant's nginx proxy App configuration and docs
  use existing `/ssl` certificates and configurable host ports; HA Ops terminates
  its optional TLS independently with Python's standard library.

Portable tests cover trust, exact routing, token persistence, fail-closed private
storage and diagnostic cache behavior. Browser/Linux fixture results are separate
qualification evidence; unit tests alone do not establish an outage exercise or
production adapter qualification.

Ingress forwarded-context evidence: Home Assistant Core
`homeassistant/components/hassio/ingress.py::_init_header` supplies ingress path,
forwarded host/protocol; Supervisor `supervisor/api/ingress.py::_init_header`
forwards that context and overwrites user identity headers. These values are
read only after the actual TCP peer is verified.

## Verification

Run portable/auth/socket diagnostics with `python3 -m pytest -q
ha-ops/tests/test_direct_auth.py ha-ops/tests/test_direct_access.py
ha-ops/tests/test_native_status.py`. Run the same paths through the repository's
`dev/virtualization/ha_ops_dev.py run --input . --` wrapper for the genuine
RecoveryStore allocation/cache/corruption case.

The visible browser fixture is `ha-ops/tests/browser/direct-access.mjs`. Run it
through the shared `playwright-node` launcher with `HA_OPS_BROWSER_CDP_URL`
pointing to the existing dedicated persistent-profile browser and
`HA_OPS_BROWSER_ARTIFACTS_DIR` set outside product inputs. It starts the existing
network-none Linux runner and a temporary localhost direct/ingress fixture.
A nonce-owned file bridge under the runner output bind carries only genuine
Linux NativeStatus projections and explicit fixture start/stop signals. No
production network path, binding or execution adapter is added.

The Linux fixture installs a genuine RecoveryStore point. Its independent worker
stops fixture services, makes one durable file effect and exits before journal
ACK. The browser observes the pending recovery journal and genuine unhealthy
service diagnostics after ingress loss, then replaces the entire owned host
fixture process with the same private token data and direct port. Repeated
refreshes must preserve the Linux effect and service-command counters. The test
also checks HTTP persistence, optional HTTPS Secure cookies and a second tab.
Screenshots and a nonsecret report describe executed proof; fixture implementation
alone is not a successful run. This remains isolated qualification with fake Core
service capabilities, not live HA or a production native adapter.

The runner closes only recorded tabs, signals its exact Linux bridge to finish,
and requires the VM runner to remove its owned container and output bind.

The fixture deliberately does not launch a browser process. Its CDP connection
is disconnected after owned-tab cleanup, preserving all existing pages and
profile data. Missing CDP configuration fails immediately before fixture launch.
This avoids ambiguous ownership of restored or startup tabs.

Authenticated dormant migration requests keep the five-second socket I/O timeout
and a ten-second absolute intake deadline. The protected adapter releases only
the intake deadline after complete framing/body/schema, current authentication,
Origin and context CSRF validation. GET diagnostics have the same handoff after
authenticated header validation. Long domain review/decision/content work can
then finish without the intake timer closing its connection. Expired intake
never enters the domain, and a previously queued timer callback cannot close a
request after the synchronized handoff. Other direct routes retain their
existing ten-second request deadline. The handoff grants no migration consent.
