# Nem-sei V2 architecture

V2 is a Flask modular monolith. Web, scheduler, and one worker run as separate
processes against one V2-only SQLite database. Routes call services, services
call repositories, and repositories call the database. Scheduler only enqueues
jobs; worker claims and executes them. V2 never imports V1.

Persist timestamps in UTC and render dates/times in Europe/Lisbon at the UI
boundary. V2 migrations are Alembic-only and run explicitly before application
roles start.

## Deployment and SQLite operating rules

Production and preview start only through `scripts/v2_compose_up.sh`; direct
`docker compose up` is unsupported because it bypasses host-side bind-mount
validation. Those deployments use the fixed host root
`/opt/server/apps/Nem-sei-v2-data`, physically separate from V1's
`/opt/server/apps/Nem-sei/data`. Local development must opt in with
`NEMSEI_V2_DEPLOYMENT_MODE=development` and an explicitly chosen, disjoint
host root.

The canonical script validates real paths and the rendered Compose mount before
running the one-shot `migrate` role, then starts web, scheduler, and exactly one
worker. SQLite is the production backend for this foundation: multiple workers,
worker scaling, and worker concurrency are unsupported and rejected by the
deployment wrapper.
