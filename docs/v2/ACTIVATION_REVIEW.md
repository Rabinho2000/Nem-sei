# Controlled provider activation and mapping review

V2 does not activate imported identities automatically. Operators review the
canonical asset, mapping evidence, and provider connection separately.

## Connection safety

The connection workflow stores only a credential-reference name. It never
accepts or renders passwords, bearer tokens, app secrets, endpoints, or raw
provider payloads. Configuration, connection enablement, and the global
provider_reads capability are independent gates. Enabling a connection does
not make a provider call possible by itself.

The connection page shows implementation-supported capabilities separately from
runtime-authorized capabilities, plus sanitized integration-health and
rate-limit state. Imported legacy connections remain disabled until an operator
configures and enables them explicitly.

## Mapping review

/mappings groups and filters mappings by provider, connection, status,
organization, asset, and identity-review findings. Each row exposes the
canonical asset, owner, provider identifier/name, aliases, and legacy import
evidence without exposing secrets.

Approval is transactional and audited. It requires a reviewed canonical asset,
an enabled/configured connection, a non-empty external identifier, no active
connection-scoped claim conflict, and no quarantined identity evidence.
Rejection marks a pending mapping invalid. No action silently merges assets,
repairs conflicts, or activates a batch.

## Temporal source policy

/source-policies creates explicit monitoring or production policies for
approved active mappings. Policies carry priority, fallback intent, and
validity dates. They are never fabricated from imported mappings. Equal
priority primary sources are surfaced as reconciliation conflicts; historical
rows remain unchanged when a later policy is added.

Policy date evaluation uses the asset's explicit IANA timezone. An unknown or
invalid timezone is a blocking finding for source-day-dependent production and
is never replaced with a global Lisbon default.

## Activation preflight

/mappings/<id>/preflight performs a provider-neutral, network-free check for
the requested capability. It reports implementation support, runtime
availability, connection and mapping state, source-policy validity, timezone
requirements, global safety gates, and integration-health findings.

Provider validation is operator-initiated only. The current deployment keeps
provider_reads=false, so preflight blocks before any HTTP call. Mutations,
notifications, and report distribution remain denied.

When all gates are ready, the preflight page exposes one explicit
``/mappings/<id>/validate`` action. It requires exactly one active FusionSolar
mapping on the connection, validates authentication and the mapping against a
small discovery page, then runs current monitoring and one provider-local
production day. The action never approves mappings, creates source policies, or
starts scheduler-wide work. Each request and outcome is sanitized in the
operator audit log; provider request and SyncRun evidence remains in the
existing integration tables.
