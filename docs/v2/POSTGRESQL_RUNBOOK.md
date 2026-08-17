# PostgreSQL V2 runbook

V2 operational data lives only in PostgreSQL 16.11, pinned in Compose to
`postgres:16.11-bookworm@sha256:a2420e9555e2224583fe84d0bb3f0b967e69354ae3a0be55a9c14e251388c4eb`.
The PostgreSQL service has no published host port and its data lives in the
`nemsei-v2-postgres-data` named volume. SQLite is restricted to frozen V1 and
the V1 importer's read-only source database/fixtures.

## Configuration and startup

Create two mode-0600 host secret files outside Git: one containing the
PostgreSQL password and one containing the complete V2 SQLAlchemy URL, for
example `postgresql+psycopg://nemsei:<password>@postgres:5432/nemsei_v2`.
Set their paths in `.env.v2` through
`NEMSEI_V2_POSTGRES_PASSWORD_SECRET_FILE` and
`NEMSEI_V2_DATABASE_URL_SECRET_FILE`. Compose mounts them as Docker secrets;
the application only receives `/run/secrets/v2_database_url`.

Start with `scripts/v2_compose_up.sh`. It verifies V1/V2 host-artifact roots
are disjoint, starts PostgreSQL, waits for its health check, runs the explicit
one-shot Alembic migration, and only then starts web, scheduler, and the single
worker. Normal roles never migrate on startup. `/healthz` is liveness-only;
`/readyz` performs a PostgreSQL read/connectivity check plus Alembic revision
comparison and does not write application data.

Pool and timeout defaults are intentionally small and role-specific. Web,
scheduler, worker, migrate, and administrative roles select conservative
defaults; every pool and timeout setting can be overridden with
`NEMSEI_V2_DB_*` configuration. Network/provider work must remain outside
database transactions.

## Backup, restore, and rollback

Set `NEMSEI_V2_HOST_DATA_ROOT` to the dedicated V2 host artifact root. Run
`scripts/v2_postgres_backup.sh` daily. It creates a PostgreSQL custom-format
backup under `<v2-root>/backups` and removes V2 backup archives older than
seven days. It never accesses V1 data.

Test a backup before relying on it:

```text
scripts/v2_postgres_restore_smoke.sh /absolute/path/to/nemsei-v2-<timestamp>.dump
```

The smoke test restores into a newly created disposable database, verifies the
Alembic head and reads representative queue data, then drops only that
disposable database. For operational rollback, stop V2 roles, restore a known
good logical backup into a freshly created PostgreSQL database, point the V2
secret URL at that database, verify `/readyz`, then restart through the
canonical startup wrapper. Do not attempt to recover V2 by reusing V1 SQLite.
