# Nem-sei V2 roadmap

`GOAL.md` states the product intent. This document maps that intent onto the
repository as it actually exists, sequences the remaining work, and records who
owns which milestone. `DECISIONS.md` holds settled architectural decisions and
`KNOWN_GAPS.md` holds the honest capability boundary of the current build.

Last reviewed: 2026-08-19.

> **Milestone numbering.** This table previously numbered portfolios M5 and
> reporting M8, while `REPORTING_PARITY.md` numbered reporting M5 with phases
> M5.1–M5.7. The reporting numbering is the one in active use and the table now
> follows it. Portfolios move to M8.

## Current state

V1 is frozen (tag `v1-final-2026-08-13`) and still runs the real operation on
port 5000: 267 assets, 214 customers, ~55k production records, ~29k Telegram
alerts, 5.4 GB of runtime data. V1 remains the source of truth until an explicit
cutover.

V2 runs on port 5002 from this worktree with PostgreSQL 16 and separate web,
scheduler and worker processes, at Alembic revision `0011_reporting_datasets`.
It holds imported identity data — 266 assets, 325 devices, 214 organizations,
134 provider mappings — the contract attributes and commercial terms imported
from V1, and daily energy facts imported from the provider payloads V1 stored.
Only one of those facts came from a live V2 provider call: the FusionSolar
canary's single corrected day. Everything else is V1 evidence, marked as such in
each fact's metadata.

That remains the most important scheduling fact in this document. The fact
tables are still effectively empty, so structural changes to the canonical model
are close to free now and will not be later — and the reporting work has just
shown exactly which structures are missing: a second energy metric, tariffs, and
commercial attributes on `Asset`.

## Goal to implementation map

| Goal section | Status | Where |
| --- | --- | --- |
| §2 PostgreSQL only, isolated, migrated | done | `db/engine.py`, `migrations/`, `scripts/verify_v2_runtime_isolation.py` |
| §3 Provider adapters behind stable interfaces | structure done | `providers/registry.py`, `integrations/fusionsolar/`, `integrations/sigenergy/` |
| §4 Synchronization as a subsystem | mostly done | `sync/`, `SyncRun`, `SyncCursor`, request states and attempts |
| §5 API efficiency | designed | per-connection daily cursor, 31-day cap, batching, one login per sync |
| §7 Current state vs historical facts | done | append-only `monitoring_observations` + `monitoring_current_states` projection |
| §15 Jobs as first-class entities | done | `jobs/` with leases, dedupe keys, attempts, append-only `job_events` |
| §19 Data quality modelled explicitly | done and enforced | `QUALITY_STATES` plus the `value IS NOT NULL OR quality = 'missing'` constraint |
| §22 Testing | good baseline | `tests_v2/`, including architecture-boundary and migration tests |
| §23 Deployment safety | done | `scripts/v2_compose_up.sh`, mounted-file secrets, separate data root |
| §18 Observability | partial | readiness compares Alembic revision to head; no provider/worker health surface, no correlated request/job/sync IDs |
| §17 Auditability | table only | `operator_audit_events` exists and is empty |
| §1 Canonical model | **incomplete** | flat `Organization → Asset → AssetAlias`; no site/plant/device hierarchy |
| §16 AuthN/AuthZ | not started | single configured administrator password hash; no users, no permissions |
| §9 Portfolios | done | `portfolios/`, `web/portfolio_routes.py`; V1's two portfolios imported with all 80 members |
| §8 Production | facts and expected production | `production_facts` holds five daily energy metrics with revision supersession; expected production comes from confirmed financial models |
| §6 §12 §13 Monitoring board, alarms, incidents | not started | — |
| §10 Reporting | asset report complete | rules, datasets, snapshots, both renderers, `reporting/assembler.py`, and the tariff/billing/contract inputs it reads; see `REPORTING_PARITY.md` |
| §11 Automated reporting | not started | — |
| §14 Automations and notifications | not started | — |
| §24 V1 migration | identity only | `MIGRATION_MATRIX.md` is still a stub; import residue unresolved |
| §26 Provider discovery | partial | FusionSolar and Sigenergy discovery exist behind capability gates |
| §27 Configuration UI | partial | provider connections and mappings are editable; schedules and thresholds are not |

## Ownership and coordination

Two agents work on this product. The worktree at `/opt/server/apps/Nem-sei-v2`
is shared through the `nemsei-dev` group; V1 at `/opt/server/apps/Nem-sei` is
frozen and must not be modified.

Rules:

- `rewrite/v2` is the trunk. Milestone work happens on `v2/<slice>` branches.
- One owner per milestone. Only the owner touches that milestone's modules.
- **M1 is owned by Claude.** While M1 is open, Codex must not modify schema,
  migrations, provider mappings, or the V1 importer.
- Never discard or revert work you did not create.
- Record every settled architectural choice in `DECISIONS.md` so the next agent
  does not re-derive it.

## Milestone sequence

| Milestone | Content | Depends on | Owner |
| --- | --- | --- | --- |
| **M1** | Canonical model extended to site/plant and device level | — | Claude |
| M2 | First real FusionSolar sync: one connection, small plant subset, read-only, end-to-end evidence | M1 | Claude (canary validated 2026-08-19; portfolio rollout needs a V2-only account — see `FUSIONSOLAR_CANARY.md`) |
| M3 | Close the identity migration: resolve conflicts, quarantines and unresolved rows; fill `MIGRATION_MATRIX.md` | — (parallel) | Claude (done, five plants pending an operator ruling) |
| M4 | Users and permissions (§16) plus audit events that actually get written (§17) | M2 | unassigned |
| M5 | **Reporting rebuilt on persisted canonical data (§10)** — its phases are M5.1–M5.7 in `REPORTING_PARITY.md` | M1 | Claude (done for the asset report and for portfolio reporting; see `PORTFOLIOS.md`) |
| M6 | Canonical production and aggregation: MTD, YTD, monthly (§8) | M2 | unassigned |
| M7 | Alarms, normalised status, operational dashboard (§6, §12) | M2, M6 | Claude (Fatia 1 **LIVE VERIFIED** 2026-08-20: `device_status_facts` imported from V1, migration `0015`, `/diagnostics` UI — see `DIAGNOSTICS.md`. Fatia 2 **LIVE VERIFIED** 2026-08-20: live FusionSolar device read, `getDevList`+`getDevRealKpi`, contract-gated, migration `0016` adds freshness/quality/provenance, canary-proven against the real account (asset 153, two inverters, 5.63 s total V1 downtime, one real defect caught and fixed before persistence, active power/day energy/state all verified, `collectTime` confirmed absent). Fatia 3 **LIVE VERIFIED, then recovered and hardened** 2026-08-20 (same day, later session): persistent, restart-safe, idempotent poll scheduling (`Job`/`ScheduleState`, no new tables), config-gated to one connection, run live for a 70 min/30 min-cadence sustained window — 3/3 cycles succeeded, 6/6 readings, 0 failures, max gap 30:03, 9/15 calls used, confirmed by a later recovery audit to have completed cleanly on schedule, not cut short (`DEVICE_TELEMETRY.md` §9.1). That audit found the real gap: the live cycles were driven by a session-local one-off process, not the standing scheduler container, and there was no hard cap in the code at all. Both fixed: `Scheduler.run_once()` now enqueues `device_status.poll` on its own when configured, gated by a persisted, tamper-evident lifetime cycle cap (§9.2-9.3). A day-plus availability canary is fully prepared (compose override, verified backup, 48-cycle cap reasoned against real account-usage evidence) but **BLOCKED** on one live-database write this session's own safety classifier correctly refused to force through — see `DEVICE_TELEMETRY.md` §10. Density is still not proven sufficient for a full operating day; availability stays off. Fatia 4 **IMPLEMENTED+TESTED** 2026-08-20: a first deterministic operational-findings layer (`diagnostics/findings.py`) — device-unavailable/unknown/no-history/stale, peer power/energy disparity, partial coverage — recomputed per page load, no persisted lifecycle yet, wired into `/diagnostics/assets/<id>`. D1 (planned in `DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`) **LIVE VERIFIED** 2026-08-21: persisted `DiagnosticIncident` (migration `0017`) with stable identity/open-resolved lifecycle/duration/provenance, evaluated periodically by the same `Job`/`Scheduler`, zero provider calls needed or made. Live against production: two real 5-minute cycles reconfirmed the same 642 real incidents (opened from V1-imported history) without duplication — `occurrence_count` 1→2, `opened_at` unchanged. Recovery-closes-the-incident proven by the automated suite against a real Postgres, not production (no real recovery to observe in the current static V1-imported data). D2 **IMPLEMENTED, TESTED, deployed** 2026-08-21: `/diagnostics` overview (worst-first, sorted before any display limit) and a new `/diagnostics/incidents` page; a real bug caught by its own search test (missing `Organization` join made every incident search return zero results, fixed). D3 **IMPLEMENTED, TESTED, closed out** 2026-08-21: `NotificationChannel`→`NotificationPolicy`→`NotificationEvent`, structural identity (`incident_id, kind, channel_id`), mock-only Telegram client (no real HTTP client exists in this codebase yet). Two real bugs caught by the required proofs: a concurrency race lost to the unique constraint aborted the whole transaction instead of a graceful no-op (fixed with a SAVEPOINT), and delivery originally shared one transaction for a whole batch, which could have resent an already-delivered message after a mid-batch crash (fixed: one commit per event, proven by a dedicated crash-simulation test). Migration `0018` applied to production; live processing left deliberately OFF. Final dry-run against the complete real universe (644 incidents, `open`+`resolved`): 0 resolved ever, 642/644 (99.7%) over 30 days old (D1 initialization materializing historical V1 import as `opened_at`-dated episodes, not new events), only 2 genuinely recent (the canary's live polling lapsing). Recommended policy reaches zero notifiable today for an honest, quantified reason (rule-code scope, not severity-tuning to hide volume) — a what-if simulation shows including the two structural rule_codes with a 24h escalation would produce 1288 messages in one pass. NO-GO for D4 still, pending explicit human approval of the proposed policy and an inert (channel-disabled) live activation first. Sigenergy has no device-level contract to build on, evidenced not assumed — see `DEVICE_TELEMETRY.md`) |
| M8 | Portfolios with validity periods (§9) | M1 | Claude (done: domain, import, aggregate dataset, a rebuilt UI, and the monthly gerar/rever/aprovar workflow — see `PORTFOLIOS.md`) |
| M9 | Automations, notifications, digests, scheduled reports (§11, §14) | M7, M8 | unassigned (the batch unit already exists: `generate_report_run` is idempotent and the workflow states are what a scheduler would read to decide whether a period needs attention) |

This sequence departs from the order implied by `GOAL.md` in one place: the
canonical model hierarchy is pulled ahead of everything else. The reason is
cost, not preference. V1 already operates at device level — it holds 325
provider devices, ~55k inverter power samples, ~51k device realtime snapshots
and ~6.7k sampled inverter availability days — so "which inverter is offline"
(§6) is a question V1 answers today and V2 currently cannot represent. Adding
that hierarchy while the V2 fact tables are empty is a migration; adding it
after monitoring and production facts exist is a data-migration project.

## M1 — canonical model extended to device level

Owner: Claude. Status: implemented on 2026-08-19, awaiting review.

Delivered: migration `0008_canonical_devices`, the `Device` model with its
service and repository surface, device-scoped provider claims, device import
from V1, and 6 new tests. The full suite is 184 passing with ruff clean, and the
migration was verified up and down against a restored copy of the live V2
database with all 265 assets, 214 organizations, 509 aliases, 134 mappings and
1205 import records intact.

### Scope

Schema, import and tests only.

### Out of scope

No provider calls of any kind. `provider_reads` stays `false`. No new sync
behaviour, no new web routes beyond what is needed to make the data visible if
that proves necessary, and no refactor of existing modules.

### Steps

1. Derive the model from V1's real device data — `provider_devices`,
   `provider_device_expected_strings`, `inverter_power_samples`,
   `device_realtime_snapshots`, `inverter_availability_sampled_daily` — and
   record the resulting decision in `DECISIONS.md` with its justification.
2. Add an Alembic migration introducing the device level, with device kind
   (inverter, meter, datalogger, string box), a foreign key to its asset,
   lifecycle status and validity periods.
3. Extend `AssetProviderMapping` to support device-level mappings with correct
   per-connection uniqueness, without weakening the existing plant-level claim
   rules.
4. Extend the read-only V1 importer to populate devices, reusing the existing
   `created` / `conflict` / `quarantined` outcome recording so nothing is lost
   silently.
5. Add tests: migration up and down, architecture boundaries, importer against
   a fixture, and device mapping uniqueness.
6. Update `ARCHITECTURE.md`, `ASSETS_PROVIDER_MAPPINGS.md`,
   `MIGRATION_MATRIX.md` and `KNOWN_GAPS.md`.

### Acceptance criteria

- `alembic upgrade head` and `alembic downgrade -1` run cleanly against both an
  empty PostgreSQL database and the current V2 database, without losing
  existing rows.
- `pytest tests_v2` passes in full, with at least four new tests covering
  devices and device-level mappings.
- Importing from `v1-identity-snapshot.db` creates devices whose count
  reconciles against V1's 325 `provider_devices`, and every divergence has a
  recorded outcome — no row disappears silently.
- One device can carry multiple external identifiers from different providers,
  with temporal validity, proven by test.
- `tests_v2/test_architecture_boundaries.py` still passes: no V1 imports, no
  business logic in routes.
- Zero provider network calls in this milestone; `provider_reads` remains
  `false`.
- `DECISIONS.md` records the model decision and the V1 evidence behind it.
- V1 is untouched: its container keeps running and nothing under
  `/opt/server/apps/Nem-sei` is modified.
- No secret is added to a tracked file; `scripts/check_tracked_secrets.py` is
  clean.

## Risk register

| # | Risk | Mitigation |
| --- | --- | --- |
| R1 | Flat canonical model versus the required hierarchy; retrofit cost grows with every fact written | M1, executed while fact tables are empty |
| R2 | Two agents on one product building competing structures | One owner per milestone; `DECISIONS.md` as the shared record |
| R3 | Long parallel run: V1 is frozen but indispensable, V2 has no facts | A written V1 hotfix exception process is still missing |
| R4 | Provider code has never run against a real API | M2 deliberately small: one connection, few plants, read-only |
| R5 | Import residue — 21 conflicts, 2 quarantines, 56 unresolved — has no owner or deadline | M3 |
| R6 | History backfill API budget for 267 assets against a 31-day cursor cap | Calculate the request budget before enabling backfill |
| R7 | Terminology collision: "capability" already means external action gates and provider abilities | Use a distinct term for user authorization in M4 |
