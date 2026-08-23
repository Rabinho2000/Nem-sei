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
