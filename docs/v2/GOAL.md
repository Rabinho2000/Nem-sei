# Nem-sei V2 goal

This is the authoritative statement of product intent for V2. It is owned by
the product owner, not by an implementing agent. `ROADMAP.md` maps this intent
onto the actual repository state and sequences the work; `DECISIONS.md` records
what has already been settled. When this document and an implementation
disagree, this document describes the target and the implementation is the
thing that must change — but only through an approved milestone, never through
an unrequested refactor.

## Vision

Nem-sei V2 is the central internal platform for managing, monitoring and
reporting the solar PV O&M portfolio. It progressively replaces fragmented
monitoring, spreadsheets, manual reporting, provider portals and repetitive
operational work.

It is built for real operational use, not as a demonstration.

The platform provides one canonical view of customers, portfolios, sites,
plants, assets, inverters, meters, dataloggers, provider mappings, current
operational state, alarms, production, historical performance, availability,
incidents, maintenance, financial and performance indicators, reports, and
scheduled automations.

## 1. One canonical data model

Provider-specific objects must not define the internal architecture.
FusionSolar, Sigenergy, SMA, ExpertCom and future providers may represent the
same physical site differently. Nem-sei maintains its own canonical entities.

A physical asset must be able to carry multiple external identifiers:

```
Canonical Site
   ├── FusionSolar station ID
   ├── Sigenergy plant ID
   ├── SMA system ID
   ├── ExpertCom identifier
   └── internal Solcor/customer code
```

External providers are data sources. They are not the domain model. The exact
entity set is derived from the real V1 data and domain, not from a preferred
diagram.

## 2. PostgreSQL as the only operational database

PostgreSQL is the only V2 runtime database. SQLite is permitted only as a
migration source, a read-only legacy source, or a test/import fixture. The V2
database is isolated from V1. All schema evolution goes through migrations, and
the system must support clean creation, migration, a rollback strategy,
constraints, indexes, foreign keys, transactions, concurrency and historical
data.

## 3. Multi-provider architecture

First-class integrations are targeted for Huawei FusionSolar, Sigenergy, SMA,
and ExpertCom where relevant. Adding a provider must not require modifying the
whole application.

```
Provider API → Provider Adapter → Sync / Ingestion → Canonical Data → Services
```

Provider-specific logic stays inside provider modules. Constructs such as
`if provider == "huawei":` must not spread through the application.

## 4. Reliable synchronization engine

Synchronization is a first-class subsystem, not a set of scheduled calls. It
supports SyncRun, SyncCursor, SyncJob, provider account state, last successful
and last attempted synchronization, retries, exponential backoff, API
quota/budget tracking, rate limiting, idempotency, reconciliation, backfill,
partial failures, completeness, stale-data detection, structured errors and
audit history.

A failed synchronization must not corrupt valid data. A retry must not create
duplicate facts.

## 5. API efficiency

Provider API requests are expensive resources. The platform avoids unnecessary
polling and repeated identical historical requests, caches appropriately,
persists cursors, understands different refresh frequencies, batches where the
provider allows it, and distinguishes current state from immutable historical
facts.

Historical production for March 2026 must not be downloaded every five minutes
in August 2026.

## 6. Canonical monitoring

Monitoring is provider-independent and must eventually answer: is this site
operating; is communication available; which inverter is offline; how long has
it been offline; is the alarm persistent; is it recurring; when did the state
change; is the issue new or acknowledged; is monitoring data stale; is data
missing because the provider is unavailable.

Internal status must not merely mirror a provider status string. Status
normalisation is explicit and auditable.

## 7. Historical facts

Current state, events and historical facts are separate concerns.

```
Current:  Inverter 03 = Offline
History:  14:05 Online → 14:32 Offline → 15:10 Online
```

History must not be lost when current state changes. This applies to status,
alarms, production, availability, communication state, and incidents where
relevant.

## 8. Production data

Production is a canonical platform capability: daily, monthly, annual,
cumulative, per inverter, per plant, per portfolio. The platform progressively
supports MTD, YTD, expected production, P50/P90 where applicable, deviations,
performance ratios where data allows, availability, losses, and missing-
production detection.

Provider APIs populate production facts. Reports query persisted facts. Reports
must never call provider APIs directly.

## 9. Portfolio management

Portfolio is a real domain concept, not a reporting filter. A site may belong to
a customer portfolio, an internal operational portfolio, a reporting portfolio,
or another logical grouping. Membership has validity periods.

```
Site A → Portfolio X   2026-01-01 → 2026-06-30
Site A → Portfolio Y   2026-07-01 → ...
```

Historical reports respect the membership valid at the reporting date.

## 10. Reporting engine

Reporting covers site reports, portfolio reports, monthly reports, operational
reports, production reports, performance summaries and custom exports.

Reports are reproducible: the same report for the same period and data version
produces consistent values. Report generation uses canonical persisted data and
is never designed around live API availability.

V1 reports are a reference for calculations, KPIs, layouts, graphs, tables and
customer requirements. V1 code is not copied blindly.

## 11. Automated reporting

Scheduled monthly and portfolio reports, configurable recipients and templates,
automatic generation, delivery status, retry on failure, history, and preview
before activation.

```
Portfolio: Customer A
Every: 1st business day of month
Generate: previous month's portfolio report
Recipients: operations@... , customer@...
```

Failures must be visible.

## 12. Monitoring board

The monitoring board becomes a proper operational dashboard answering one
question: what requires attention now.

Candidate states: Operational, Warning, Error, Disconnected, Persistent Error,
Recurring Error, Data Stale, Provider Error, Unknown.

Useful filters: customer, portfolio, provider, site, severity, duration,
status, assigned person, acknowledged/unacknowledged.

Operational usefulness is prioritised over visual complexity.

## 13. Incident / issue lifecycle

Monitoring problems must be able to become operational issues:

```
Alarm detected → Issue created → Acknowledged → Investigating
   → Technician visit → Resolved → Verified
```

This need not be implemented immediately, but the architecture must not
prevent it.

## 14. Automations

The platform progressively eliminates repetitive O&M work: persistent and
recurring alarm detection, offline inverter detection, communication-loss
alerts, missing-production alerts, stale provider data alerts, automatic
reporting, daily operational digest, Telegram/email notifications, maintenance
reminders, data reconciliation, and failed-sync alerts.

Automations run independently of HTTP requests. Critical background execution
must never be embedded in Flask web workers.

## 15. Job architecture

Background jobs are first-class entities supporting scheduled execution,
retries, status, timestamps, error details, idempotency, observability, safe
manual rerun, and an audit trail.

The web application is not the scheduler. A web restart must not create
duplicate scheduled tasks. Multiple web workers must not accidentally run the
same scheduler.

## 16. Authentication and authorization

V2 requires proper authentication and capability-based authorization, with the
default policy:

```
DENY unless explicitly allowed
```

Candidate capabilities: view monitoring, manage sites, manage portfolios,
configure providers, trigger synchronization, generate reports, manage report
schedules, manage users, manage integrations, access financial data, administer
platform.

Role checks must not be scattered. Prefer `user.can("provider.configure")` over
`if user.role == "admin":`.

Naming note: the codebase already uses "capability" for external action gates
(`CAPABILITIES`) and for provider abilities (`ProviderCapability`). User-facing
authorization must use a distinct term to avoid a third meaning of the same
word.

## 17. Auditability

Important operations are traceable: mapping changes, provider credential
changes, manual sync triggers, report generation, report schedule changes,
portfolio membership changes, alarm acknowledgement, configuration changes.

Where reasonable, store who, what, when, target entity, previous state and new
state.

## 18. Observability

Failures must be visible: structured logs, request IDs, job IDs, sync run IDs,
meaningful exceptions, health and readiness endpoints, and provider, database,
worker and scheduler health.

It must be possible to determine why data is missing.

```
Bad:    No production available
Better: Production unavailable because Huawei sync failed at 14:32.
        Last valid data: 13:45. Retry scheduled.
```

## 19. Data quality

Data quality is modelled explicitly: complete, partial, stale, missing,
estimated, provider unavailable, invalid, pending reconciliation.

`0 kWh produced` and `production data unavailable` are operationally different
and must never be conflated.

## 20. Performance

Avoid the problems accumulated in V1: N+1 queries, huge ORM object graphs,
repeated calculations, unnecessary provider calls, slow report queries,
unindexed time-series queries, and missing pagination. Large portfolios must
remain usable.

## 21. Clean modular architecture

There must never be another giant `app_factory.py`. Responsibilities are
separated by domain module. The concrete structure is derived from the actual
project rather than copied from a template.

Business logic must not live primarily in HTTP route handlers, templates,
provider adapters, or large utility files.

## 22. Testing

Testing is mandatory and grows progressively: unit, database, integration,
provider adapter, sync, authorization, reporting and migration tests.

Provider APIs must be mockable. Tests must never use production credentials or
touch production databases. Test storage is physically isolated.

## 23. Development and deployment safety

V1 runs in production. V2 development must not break it. Avoid shared mutable
databases, shared Docker volumes, conflicting ports, destructive migrations,
restarting unrelated containers, and deleting V1 data.

V2 owns its own web service, worker, scheduler, PostgreSQL database, volumes,
configuration and health checks. Infrastructure is never added because it is
fashionable; every dependency needs a reason.

## 24. Migration from V1

V1 holds valuable real operational data, so migration is deliberate rather than
wholesale. For each domain, determine what exists, what is trustworthy, what
maps cleanly, what needs transformation, what stays legacy-only, and what
should be regenerated from providers.

Migration is repeatable, testable, resumable where possible, non-destructive,
logged and validated. V1 remains the source of truth until an explicit cutover.

## 25. Preserve useful functionality

Analyse V1 and keep what is conceptually valuable: FusionSolar, Sigenergy and
ExpertCom integration behaviour, monitoring, Telegram, reporting, portfolio
reports, exports, scheduled tasks, historical data, plant discovery, automatic
provider discovery, and report queue/scheduler concepts.

Preserve useful behaviour. Do not preserve bad architecture for compatibility.

## 26. Provider discovery

Discovering plants from a provider account must be a supported workflow:

```
Add account → Test credentials → Discover stations → Show unmapped stations
   → Map to existing canonical Site  OR  Create new Site
```

The same workflow applies to every provider. Manual database editing must never
be required.

## 27. Configuration UI

Operational configuration moves out of code and `.env` where practical.
Administrators configure provider accounts, site mappings, portfolios, report
schedules, notification settings, recipients, thresholds and automation rules.
Secrets remain handled securely.

## 28. UX objective

The interface is an engineering/O&M tool. Prioritise clarity, speed, useful
information density, obvious error states, good search and filtering, few
clicks, and responsive behaviour. Avoid decorative dashboards and unnecessary
animation.

## 29. Main operational outcome

A user opening Nem-sei in the morning should immediately understand:

```
134 plants monitored

126 Operational
  3 Warning
  3 Error
  2 Communication Lost

4 new incidents
2 persistent incidents
1 provider synchronization problem

Yesterday: 421.6 MWh generated
3 sites below expected production

2 monthly reports awaiting review
7 reports automatically delivered
1 sync job failed
```

That is the level of operational overview the product aims for.

## Engineering principles

```
correctness               > speed of implementation
canonical persisted data  > live provider response
explicit boundaries       > convenience coupling
idempotent jobs           > fragile scheduled scripts
default deny              > implicit access
repeatable transformation > manual edits
visible failure           > silent failure
```

## Working rules for implementing agents

- Inspect the repository, documentation, `git status`, uncommitted changes and
  recent history before changing anything.
- Never discard, overwrite or revert changes you did not create unless
  explicitly instructed.
- Never touch production data destructively.
- Never expose or commit secrets.
- Do not start a rewrite from this document. Parts of it are already
  implemented; find them first and extend them.
- Do not create a parallel competing architecture, and do not duplicate
  existing work.
