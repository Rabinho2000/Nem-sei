# FusionSolar single-account ownership window

Mechanism that lets V2 borrow the shared FusionSolar account from V1 for a
short, bounded, auditable test window, without a second account and without
changing V1's code.

Tool: [`scripts/fusionsolar_ownership_window.py`](../../scripts/fusionsolar_ownership_window.py).

## Why this works with zero V1 code changes

V1 already serializes every FusionSolar API call it makes from a background
job (state sync, production sync, backfill, month cycle/close, report
requests, wat requests -- all of `FUSIONSOLAR_BACKGROUND_JOB_TYPES`) through a
persisted, restart-safe, per-account lease:
`provider_api_account_state` in V1's SQLite database, written by
`reserve_account_lease` / `release_account_lease` in
`Nem-sei/monitoring_board/services/production_api_queue.py`, and read before
every `execute_queued_fusionsolar_*_call` in `app_factory.py`. When the lease
is held by someone else, V1 raises `ApiSlotUnavailableError`, which V1 already
treats as a normal `waiting_api_slot` backoff -- no crash, no new code path,
no user-visible error banner (the UI cooldown banner is driven by a separate
`cooldown_until` field, only set on real HTTP 407s).

This tool speaks that exact same protocol directly against V1's live SQLite
file. It does not import V1's code (avoids any process/venv coupling); it
ports the two functions byte-for-byte and verifies the expected schema before
touching anything (fails closed if V1's schema ever changes underneath it).

## Audit findings (2026-08-23)

- **Auth**: `POST {base_url}/thirdData/login` with `userName` + `systemCode`
  (password), session + `XSRF-TOKEN` cached in-process for ~55 min
  (`FUSIONSOLAR_SESSION_CACHE`, in-memory only, not restart-safe -- fine,
  since V1 just re-logs in after a restart).
- **Jobs / scheduler**: APScheduler, single in-process Gunicorn worker,
  `background_jobs` table (SQLite) is the durable, restart-safe queue.
  FusionSolar job types: `fusionsolar_state_sync` (hourly),
  `fusionsolar_production_sync` (daily 00:10), `fusionsolar_inverter_availability_backfill`
  / diagnostics (daily 12:30), `fusionsolar_month_cycle`, `fusionsolar_month_close`
  (days 1-5), `fusionsolar_report_production_request`, `fusionsolar_report_wat_request`,
  `fusionsolar_realtime_materialize_cleanup`.
- **Concurrency, three independent layers**:
  1. `FUSIONSOLAR_SYNC_LOCK` -- in-process `threading.Lock`, guards duplicate
     runs of the *same* sync function only. Invisible outside the V1 process;
     irrelevant to V2 coordination.
  2. **`provider_api_account_state` account lease** -- the real cross-call
     mutex, keyed by `sha256("fusionsolar|{username}|{base_url}|account")[:24]`.
     TTL-based (`lease_seconds`, default 300s in V1), auto-recovered on
     expiry (`recover_expired_leases()` at scheduler startup, and any future
     `reserve_account_lease` treats an expired lease as free). **This is what
     the ownership window joins.**
  3. `production_api_queue_state` -- per-`api_area` (production_kpi,
     wat_history) daily budget + min-interval throttle, independent of the
     account lease. Currently `daily_budget=20` (production_kpi, min interval
     65s) and `daily_budget=36` (wat_history) in production. The ownership
     window does not touch this table; it only prevents concurrent access,
     it does not grant extra quota.
- **Cooldown**: `api_call_state` / `provider_api_account_state.cooldown_until`,
  set for ~60 min on a real 407, independent of the lease, shown as a UI
  warning banner. The ownership window respects it (an active cooldown denies
  acquisition, fail-closed) but cannot clear it.
- **Credentials**: plaintext in SQLite `integration_configs.username`/`password`,
  editable only through the existing authenticated `/integrations` route.
  Never read or logged by this tool.
- **Known gap, not fixed (would require touching V1)**: the admin
  "Testar ligação" button (`test_connection` / `test_fusionsolar_connection`)
  calls the API outside a background-job context and does **not** check the
  account lease. Operational rule: don't use that button during an active
  ownership window. Documented, not hidden.
- **V1 database**: `/opt/server/apps/Nem-sei/data/monitoring_board.db`, WAL
  mode, `busy_timeout=5000`, group `nemsei` read-write -- safe for a second
  well-behaved writer touching only the lease columns.

## What's implemented

- `status` -- read-only.
- `acquire` / `release` -- the manual "dar ownership à V2" / "devolver
  ownership à V1" operation.
- `run-window -- <command>` -- acquires, renews continuously (~60% of TTL) in
  a background thread, runs a command with ownership held, always releases in
  a `finally` (including on timeout or exception), and writes a JSONL audit
  trail to `runtime/fusionsolar_ownership/audit.jsonl` (git-ignored;
  account_key only, never credentials).
- `selftest` -- exercises the full state machine (deny-while-held,
  auto-recovery of an expired lease, deny-in-both-directions, clean handback,
  cooldown fail-closed) against a throwaway SQLite file. Zero V1 contact,
  zero network.

## Verification so far

1. **`selftest`** (throwaway DB): all 7 checks pass -- V1-shaped schema
   verifies; V2 denied while V1 holds the lease; V2 acquires once V1's lease
   naturally expires; V1 denied while V2 holds the window; V1 resumes
   immediately on release; acquire fails closed during a simulated cooldown.
2. **`status`** against the real, live V1 database (read-only): confirms the
   real `account_key` (`a472ce0d891b8199f3b9a33c`), confirms schema matches,
   confirms no lease/cooldown was active and no FusionSolar jobs were in
   flight at the time of the check.
3. **`run-window` rehearsal against the real, live V1 lease table** (a real
   write/release against production's actual coordination row, zero
   FusionSolar network calls): takeover in **0.006s**, held for 3.03s while a
   placeholder command ran, released cleanly, `v1_resumed_cleanly: true`.

4. **Real live-network canary**, run 2026-08-23 20:16:41+01:00, with explicit
   user go-ahead: `run-window` acquired the real V1 lease in **0.007s**, and
   while held, triggered `FusionSolarDiscoveryService.validate_connection(3)`
   for real inside the running `nemsei-v2-worker-1` container (V2's actual
   production code path, not a standalone script) -- **2 real HTTP requests**
   (`/thirdData/login`, `/thirdData/stations` page 1), **status: success**,
   100 plants returned, both recorded as `ProviderRequestAttempt` rows under
   V2 sync run 14. Window held for 2.33s, released cleanly,
   `v1_resumed_cleanly: true`. V1's own `production_api_queue_state` counters
   were unchanged afterwards (0/20 production_kpi, 4/36 wat_history, no
   cooldown) and zero V1 FusionSolar jobs were observed in flight before,
   during, or after.

   Credentials for this one call were supplied by the user directly in chat
   at the point of explicit approval, immediately written to a
   session-scratch file (`chmod 600`), `docker cp`'d into the worker
   container as `_FILE`-referenced secrets (never as a `-e VAR=value` CLI
   argument, never logged, never included in this tool's audit trail beyond
   the account_key hash), and shredded from both host and container right
   after the run.

   **Independent finding, not part of this mechanism**: V1's own
   `integration_configs.password` for FusionSolar is an empty string in this
   deployment's live database -- V1 here currently has no wired credential of
   its own, which is presumably why its `production_api_queue_state` counters
   were already stale (last touched 2026-08-21) rather than reflecting daily
   use. Worth a separate look; not something this tool can or should fix.

## Persistent deployment (2026-08-23, portfolio rollout)

The broker now runs as its own standing container
(`docker-compose.v1-ownership-broker.yml`, project `nemsei-v1-fusionsolar-broker`),
published on the Docker bridge gateway address (`172.17.0.1:8765`, **not**
`127.0.0.1` -- that binds to host loopback only and is unreachable from
other containers; found live when `worker`'s first real request timed out).
`worker`/`scheduler` reach it via `extra_hosts: host.docker.internal:host-gateway`
+ `NEMSEI_V1_OWNERSHIP_BROKER_URL=http://host.docker.internal:8765` in
`docker-compose.v2.yml`. `FusionSolarRequestController.call()`
(`request_control.py` + the new `v1_ownership.py`) now acquires this lease
around every real HTTP call automatically, fails closed if the broker is
unreachable, and this has been verified live: `docker exec`-ing into
`nemsei-v2-worker-1` and calling the broker's `/status` and `/budget`
through `v1_ownership._request()` returned V1's real, current state.

Rollout progress: `scripts/fusionsolar_rollout_stage.py` (budget guard →
broker acquire → bulk timezone fix → deterministic mapping activation →
live sync → release) took the portfolio from 2 to **3 active mappings**
(added asset 2, Florineve) and fixed timezone on all 134 legacy-mapped
assets in one evidenced pass (zero previously had it except the M4 canary
asset). A static, hand-verified exclusion list
(`V1_NON_ACTIVE_ASSET_IDS`) keeps the 10 assets whose V1 `status_detail`
was not `ACTIVE` (1 cancelled, 3 on hold, 6 unclassified) out of automatic
activation, since V2's own `lifecycle_status` never inherited that V1 field
(it is `'unknown'` for all 134).

### Stopped here: a real FusionSolar login rate limit, not a bug in this mechanism

The third stage's live sync came back `rate_limited` on **both** monitoring
and production, and `provider_request_states` showed the block is on the
**`authentication`** endpoint family specifically -- login itself, not a
data-call budget. V1 never exhausts this because it caches a session for
55 minutes (`FUSIONSOLAR_SESSION_CACHE`); V2's `FusionSolarClient.authenticate()`
has no such cache, so every sync call (monitoring, production, discovery,
each built with a fresh client) re-authenticates from scratch. Across this
session's testing the account had accumulated 13 login attempts before the
provider pushed back -- something V1's architecture would never do for the
same amount of work.

This is exactly the class of finding the rollout plan says must halt
progression ("rate-limit/cooldown inesperado"), so the stage-4 (5 active)
run was not attempted. V1 itself was unaffected throughout -- confirmed via
`/status` before, during, and after every attempt: lease always clean,
zero V1 jobs ever observed in flight. Two concrete follow-ups before
scaling further:

1. Give `FusionSolarSingleAssetValidation`/`FusionSolarDiscoveryService`/
   monitoring/production services a shared, cached, reusable session the
   way V1's `FusionSolarClient` already does, so a stage that touches N
   assets costs one login, not N. Without this, 5/20/50/full-portfolio
   stages will keep tripping the same limit long before any data-call
   budget matters.
2. Wait out the provider's login cooldown before retrying -- its duration
   is not documented anywhere and `provider_request_states.cooldown_until`
   for this attempt was set to the attempt's own timestamp rather than a
   future one, so it is not usable as a "safe to retry at" signal. A
   conservative wait (hours, not minutes) is the fail-closed choice here.

`docker-compose.v2.yml` was also missing `NEMSEI_V2_FUSIONSOLAR_PRIMARY_PRODUCTION_TIMEZONE`
/`_UNIT` (the M4-canary-verified UTC/kWh contract) until this pass added
them -- without it every production sync failed closed with a
`configuration` error, which is what happened on the first stage-3 attempt
before the fix.

## Third canary attempt, ~12h later (2026-08-24) -- still blocked, now classified

Redeployed via the canonical wrapper first (the `PRODUCTION_TIMEZONE`/`_UNIT`
env vars had been in `docker-compose.v2.yml` since the previous session but
were never actually applied -- the containers had only restarted via a host
reboot, which reuses the existing images/config, not a real `docker compose
up`). Confirmed present after redeploy.

One controlled attempt (monitoring + production + device_status, one shared
session cache, one ownership window) ~12h after the first rejection and
~10h after the second: **still `rate_limited` on login**, both monitoring's
and production's authentication attempts genuinely rejected
(`actual_call_count` 14->16, `last_success_at` still frozen at the original
2026-08-23 19:16:42 login). Session reuse behaved correctly throughout --
zero duplicate logins, the failed attempt was never cached (matches
`test_a_failed_login_is_never_cached`), no retry storm. `device_status`
additionally failed with `configuration`: `NEMSEI_V2_FUSIONSOLAR_PRIMARY_
DEVICE_POWER_UNIT`/`_DEVICE_ENERGY_UNIT` were never configured -- a
separate, not-yet-fixed gap, moot until login itself works.

**One important new signal**: V1's own `production_api_queue_state` showed
`wat_history` `daily_call_count: 2` for *today* (2026-08-24) at the time of
this attempt -- V1 successfully called FusionSolar today. This does not
prove the *login* endpoint itself is open, since V1 renews its session only
every ~55 minutes and may not have needed a fresh login in that window; it
does confirm the account and password are not disabled or globally locked
-- only new-login creation is being refused.

**Classification**: `ProviderErrorCode.RATE_LIMITED` (client.py: FusionSolar
failCode 407 or a rate/call-limit message), specifically on the
`/thirdData/login` call, not a credential rejection (would classify as
`AUTHENTICATION`) and not an account lock. No `Retry-After`-equivalent
signal was ever returned (`cooldown_until` keeps landing on the attempt's
own timestamp), so there is no machine-readable "safe to retry at" value --
this is the provider withholding that information, not a gap in this
codebase's handling of it. Most likely explanation given the evidence: a
cumulative new-login ceiling (not a simple rolling cooldown window, since
12h > any duration V1's own code has ever assumed) that our test burst
across 2026-08-19/20/23 exhausted, on a reset schedule this session cannot
observe from outside. Per the operator's explicit instruction, no further
attempt was made. Real rollout stages remain stopped until either enough
time passes or the account owner has better information about FusionSolar's
actual login-rate policy for this account.

## Fourth attempt, a second/different Northbound API credential (2026-08-24)

To isolate whether the block was specific to the `primary` credential's own
login history, the operator provisioned a genuinely separate FusionSolar
Northbound API user (`solcor_om_api2`, same SOLCOR company, its own
deadline/scope) and one controlled attempt was made with it (username/
password overridden only for that one process via docker secret files,
never touching the standing containers' committed config): **same result --
`rate_limited` on login**, for an account_key that had never been used
before (`d7092...` vs the original `a472c...`), which had never held a
lease, cooldown, or made a single prior call.

This is the decisive finding: **the block is not tied to a specific
credential's login history.** A brand-new Northbound API user, on its first
ever login attempt, was rejected the same way. That rules out a simple
"too many logins on this specific account" ceiling and points instead at
something scoped above the individual API user -- most likely the source
IP (this server's outbound address) or the SOLCOR company/tenant as a
whole, at Huawei's API gateway. Neither V1 nor V2's code can see or control
either of those. A third credential would not be expected to behave any
differently and was not tried, per the same one-attempt discipline.
V1 was unaffected throughout (different account_key, clean handback
confirmed on both keys immediately after).

## Public research on the login block (2026-08-24)

Since a second, brand-new credential failed identically (previous section),
researched Huawei's own documentation and other integrators' real-world
experience with this exact API:

- FusionSolar's documented `failCode 407` (`ACCESS_FREQUENCY_IS_TOO_HIGH`)
  is officially specified as **5 login attempts per northbound user per 10
  minutes** -- [Huawei support: "Why Does the Northbound API Return Error
  Code 407 or 429"](https://support.huawei.com/enterprise/en/doc/EDOC1100379184/ec16189c/why-does-the-northbound-api-return-error-code-407-or-429).
  That alone cannot explain a block still standing 12+ hours later.
- The Northbound API also documents a **single login session limit per
  account**, and the standard community workaround for testing alongside a
  live production integration is exactly what was tried here: provision a
  second, identical Northbound API account
  ([meteocontrol help center](https://help-center.meteocontrol.com/en/vcom-cloud/latest/huawei-fusionsolar-api-1);
  discussed in [tijsverkoyen/HomeAssistant-FusionSolar issue #29](https://github.com/tijsverkoyen/HomeAssistant-FusionSolar/issues/29)
  and [#89](https://github.com/tijsverkoyen/HomeAssistant-FusionSolar/issues/89)).
  That workaround did not help here.

Both documented mechanisms (10-minute login throttle, per-account session
limit) are ruled out by our own evidence: the throttle would have cleared
in minutes, and a fresh, never-used second account should have bypassed a
per-account limit per the community's own standard practice, but was
rejected identically. The most consistent remaining explanation is an
**undocumented, longer-lived anti-abuse mechanism scoped above the
individual account** -- most plausibly the calling IP or the SOLCOR
tenant as a whole -- triggered by the unusually high count of distinct
login attempts (14-16) this account/IP made across 2026-08-19/20/23/24
while this session iterated on session reuse. This is inference from
public documentation plus our own two-credential test, not a confirmed
root cause; Huawei support is the only party who can confirm it and say
whether/how it can be lifted or IP-allowlisted. Neither V1's nor V2's code
can detect, wait out, or work around this from outside.

## Confirmed: the block is scoped to the server's outbound IP (2026-08-24)

Decisive test: `scripts/fusionsolar_login_probe.py` (standalone, stdlib-only,
no V2 dependency) run from the operator's own laptop -- a different
network entirely -- with the exact same `solcor_om_api2` credentials that
were rejected from this server minutes earlier. **Login succeeded
immediately**: HTTP 200, `success: true`, `failCode: 0`, valid XSRF token.

This confirms the hypothesis from the public-documentation research above:
the block is tied to this server's outbound address
(`161.230.174.153` at the time of this test, `curl https://api.ipify.org`),
not to the `solcor_om_api`/`solcor_om_api2` accounts and not to the SOLCOR
tenant generally -- both accounts remain valid and usable from anywhere
else right now. No further attempt was made from this server itself
(retrying here would just reuse the same blocked IP and produce no new
information).

**What this changes**: waiting longer from this server was never going to
resolve it, since the block is not time-based from what we can observe --
it is IP-based. The actionable paths forward are infrastructure decisions,
not code:
1. Ask Huawei/FusionSolar support to unblock or allowlist this server's
   outbound IP for the Northbound API.
2. Run FusionSolar-calling V2 processes (worker, or specifically the
   `fusion-canary` connection's calls) through a different egress path
   that is not blocked -- a proxy, a different host, or a NAT/outbound-IP
   change for this deployment.
3. Confirm with Huawei whether this is a temporary anti-abuse flag (in
   which case it may still lift on its own after enough time) or a
   deliberate, standing block that requires manual intervention to clear.

The ownership broker, session reuse, and shared budget guard all remain
correct and necessary regardless of which of the above happens next --
none of this finding changes anything about how V1 and V2 must coordinate
access to the account once V2 can reach FusionSolar from an unblocked
network path.

## Proxying through an unblocked network path (2026-08-24)

Built `src/nemsei/integrations/fusionsolar/socks5_transport.py` -- a
`FusionSolarTransport` implementation (stdlib-only: raw-socket SOCKS5
CONNECT handshake, TLS, minimal HTTP/1.1, manual cookie persistence across
calls on one instance) so the real client can route through a SOCKS5 proxy
instead of the default direct connection. The operator opened one via
`ssh -R 1080 <server>` from their workplace network while at work -- a
different egress than the server's own blocked IP.

**A raw, single-call test succeeded**: `FusionSolarClient` with
`Socks5FusionSolarTransport`, run directly on the host (not through the
worker container, which cannot reach the tunnel -- see below), logged in
and fetched a real discovery page (100 plants across 2 pages) through the
tunnel. This is the second, independent confirmation that the block is
IP-scoped, using the real production client code this time, not just the
standalone probe script.

**A full-stack run (ownership broker + session cache + real Postgres,
routed through the same tunnel) then failed again**, `rate_limited`, with
`actual_call_count` advancing by 2 (confirmed real network attempts, not
local state). The likely cause: `FusionSolarMonitoringService`'s and
`FusionSolarProductionService`'s authentication attempts landed **29
milliseconds apart** -- the first service's login failed for an unrelated
reason, session reuse correctly refused to cache that failure, and the
very next service call immediately retried a fresh login with no backoff
at all, plausibly tripping a burst-detection layer distinct from the
longer IP-scoped block. This is a real, separate finding: today's session-
cache design has no delay between "a login just failed" and "immediately
try another fresh login from the next caller," which is exactly the kind
of rapid-fire pattern an anti-abuse system would flag on its own. Worth a
follow-up (a short backoff after a failed authentication attempt, shared
across callers via the session cache) before relying on any unblocked path
at portfolio scale.

**Container networking note**: the worker/scheduler containers cannot
reach a service bound to the host's `127.0.0.1` (SSH's default `-R` bind) --
confirmed through several attempts (a docker-published relay forwarding to
`host.docker.internal`, a `--network host` relay) -- Docker's bridge
network cannot reach a host-loopback-only service by design, and this
environment's firewalling only permits bridge-to-host traffic that Docker
itself published via `-p`, not raw host processes or host-networked
containers binding the same address. The two real-network validations
above were both run directly on the host (a throwaway venv + the real
`src/nemsei` package on `PYTHONPATH`, Postgres reached via the container's
bridge IP directly, host→container reachability being unrestricted in that
direction), not from inside the worker container. A durable fix would
need either the SSH server's `GatewayPorts` opened for a non-loopback bind,
or a small proxy service deployed the same way the ownership broker was
(its own docker-published container) -- not attempted here given the
credential-burst finding above makes it moot until that is fixed first.

No further real login attempts were made after this. Both test account_keys
confirmed clean (no lease, no cooldown) immediately after; V1 unaffected
throughout.

## Residual risks

1. Coupling to V1's internal schema/hash algorithm (not a public API) --
   mitigated by `verify_schema()` failing closed if the table/columns
   disappear or change shape.
2. The "Testar ligação" admin bypass above -- operational rule, not a code
   fix.
3. This tool is a second real writer against V1's production SQLite file.
   Mitigated by: same `BEGIN IMMEDIATE` discipline as V1 itself, only ever
   touches `lease_until`/`lease_owner` on its own row, releases are scoped by
   `WHERE lease_owner = ours`.
4. Lease TTL default here is 90s (shorter than V1's own 300s default) with
   continuous renewal, so a crashed broker self-heals within at most one
   lease period instead of five minutes.
5. Owning the account does not raise the shared daily call budget -- a
   careless canary can still push the account towards its `daily_budget` /
   provoke a 407, independent of concurrency safety.
6. V1's `production_api_queue_state.daily_call_count` only ever sees V1's own
   calls; V2's calls are counted separately in V2's own Postgres
   `ProviderRequestState`. The ownership lease stops the two from calling
   *concurrently*, but nothing today sums the two counters against
   FusionSolar's real server-side daily limit. Harmless for one short canary;
   for longer or repeated V2 test campaigns, add a combined-budget check
   before trusting either side's local counter in isolation.

## Real canary result (2026-08-23)

Acquire took 0.007s, held 2.33s, 2 real HTTP requests
(`/thirdData/login`, `/thirdData/stations`), both succeeded, V1 observed zero
jobs in flight throughout and resumed with a clear lease immediately after
release. See "Verification so far" item 4 above for full detail.

## Session reuse (2026-08-23, portfolio rollout, session-reuse priority)

Root cause of the rate limit that stopped the rollout at 3 active mappings
(see the earlier section above): every V2 FusionSolar sync built a fresh
`FusionSolarClient` and called `.authenticate()` on it, with no session
cache anywhere -- unlike V1, which reuses one login for ~55 minutes
(`FUSIONSOLAR_SESSION_CACHE`). Fixed by
[`session_cache.py`](../../src/nemsei/integrations/fusionsolar/session_cache.py):
a process-local, credential-keyed cache (default TTL 45 minutes, so V2 never
plausibly outlives a session V1 itself would already have refreshed) that
`service.py`/`monitoring.py`/`production.py`/`device_status.py` now all go
through instead of calling `client.authenticate()` directly.

**Design, matched to the stated requirements:**
- *Avoid a login per sync*: a cache hit skips the network call, the
  ownership-broker acquire, and the request-control evidence row entirely --
  invisible to everything downstream, the same way V1's own cache hit is.
- *Conservative TTL*: 45 minutes (< V1's 55).
- *Invalidate on auth failure*: `client.py`'s `_validate()` was widened to
  recognize failCode 305 / "USER_MUST_RELOGIN" regardless of which endpoint
  returned it (previously gated to the login call only) -- a session dying
  mid-use on a *data* call is now classified as `AUTHENTICATION` too, which
  is what `is_session_expiry()`/`invalidate_session()` key off of.
- *Reauthenticate only when necessary*: invalidation only forces a fresh
  login on the *next* call; it deliberately does not retry-loop within the
  same sync (a bigger, riskier change with an unclear payoff given the
  provider's real cooldown duration is undocumented).
- *No retry storms / safe concurrency*: `get_or_authenticate()` holds one
  lock across the whole "check cache, else authenticate" section per
  process, so N concurrent callers for the same account produce at most one
  login (proven under 8 concurrent threads in
  `tests_v2/test_fusionsolar_session_cache.py`).
- *Restart-safe*: the cache is in-memory only, by design -- a worker
  restart just means the next call re-authenticates once, exactly like a V1
  restart does.
- *Never exposes tokens/passwords*: the cache key is `base_url|username`
  (already only ever handled in-process); nothing here logs a token,
  cookie, or credential.
- *Broker still mandatory, budget guard still mandatory*: session reuse
  changes nothing about `request_control.py`'s ownership-lease requirement
  or the shared-budget check -- a cache hit still makes zero network calls
  (so it needs neither), and a cache miss's real login still goes through
  both exactly as before.

**Backward compatibility**: each service defaults to its *own fresh* cache
instance (`session_cache: FusionSolarSessionCache | None = None` →
`FusionSolarSessionCache()` if omitted), not a shared process-wide one --
every pre-existing test constructs a new service per call and so keeps its
original always-reauthenticate behavior with zero changes needed anywhere
in the existing test suite. Real cross-invocation reuse is opt-in at the two
places that matter: `jobs/handlers.py`'s `_execute_production` and
`_execute_device_status_poll` now both pass
`session_cache=default_session_cache()`, the process-wide singleton, so a
worker's separate job executions actually share logins the way this section
exists to guarantee.

**Proof** (`tests_v2/test_fusionsolar_session_cache.py`, all passing):
multiple `get_or_authenticate()` calls within the TTL cost one login;
crossing the TTL costs exactly one new one; a failed login is never cached;
8 concurrent callers produce exactly one login; `invalidate()` forces
exactly one fresh login on the next call; the widened `client.py`
session-expiry detection fires regardless of which endpoint returned
failCode 305; two independently constructed services (the pre-existing
default) never share a session; two services explicitly sharing one cache
log in once across both syncs. Full `tests_v2` suite run (broker URL
unset, matching CI) to confirm no regression -- see the rollout report for
the pass count.

## Real canary retry, 2 hours later: still rate-limited

Ran the same rollout stage again (target unchanged at 3 active, no new
activations) after waiting ~2 hours -- well past V1's own 55-minute session
window, the only reference point either codebase has for this account.
`logins_this_stage: 0` (the reused-session bookkeeping itself worked
correctly -- nothing tried to log in twice), but the one real login attempt
this stage made was rejected: `provider_request_states.actual_call_count`
for `authentication` moved from 13 to 14, `last_attempt_at` matched the
retry exactly, and `last_success_at` stayed frozen at the very first
successful login hours earlier. This was a genuine second rejection by the
provider, not leftover local state. Per the rollout plan's own stop
condition ("STOP se houver rate-limit inesperado"), stages 4/5 (5, 20, 50,
full portfolio) were not attempted. V1 was unaffected throughout --
confirmed clean before, during, and after via the broker's `/status`.

**What this means going forward**: the provider's login-endpoint cooldown
for this account is longer than 2 hours, or keys off a broader signal (e.g.
a cumulative/daily count of logins across all of this session's testing,
not a short sliding window) than the code assumed. Nobody should retry a
real canary against this account without either a much longer wait or the
provider's actual documented limit, whichever the account owner has.

## Production/monitoring scheduling (hardening, no network calls)

The rollout's Priority 5 gap: `production.incremental` had a working job
*handler* (`jobs/handlers.py`) but nothing ever enqueued it -- no
persistent job path fired it automatically; every real run so far was
triggered by hand (`fusionsolar_rollout_stage.py` or a manual script).
Plant-level current monitoring (`sync_current_monitoring`) has no job type
at all yet and remains that way -- adding one was judged riskier to rush
than to leave as a named, honest gap.

Closed the production half the same way M7 Fatia 3 already closed device-
status polling: `JobRepository.enqueue_due_production_incremental()`
mirrors `enqueue_due_device_status_poll()`'s exact restart-safe, idempotent,
concurrent-tick-safe shape (a `ScheduleState` row, a dedupe key, one lock
per due slot), wired into `Scheduler.run_once()` behind
`production_sync_scheduler_enabled` (default off) +
`production_sync_scheduler_connection_id` (required when enabled -- there
is structurally no "sync every FusionSolar connection" mode, so turning
this on always names exactly one connection, matching the same restraint
device-status polling already enforces for a shared, rate-limited account).
No lifetime cycle cap, unlike device-status polling -- this is meant to run
indefinitely once enabled, the same way V1's own daily
`fusionsolar_production_sync` never had one either.

Proven with the same test shapes `test_scheduler.py`/`test_jobs.py`/
`test_config.py` already use for device-status polling (idempotent
scheduling, restart survival, one job per concurrent tick, positive-
interval requirement, config validation, env parsing) -- all against a real
Postgres, zero network calls, zero real FusionSolar credentials involved.
Deliberately **not enabled** anywhere yet: turning
`production_sync_scheduler_enabled` on for connection 3 is a one-line
compose change left for whoever decides it's safe to have this run
unattended against the still-unresolved login rate limit above.
