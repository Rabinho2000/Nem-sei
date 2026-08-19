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

Compose interpolates `.env.v2`, so every literal `$` in a value must be written
`$$`. This matters most for `NEMSEI_V2_ADMIN_PASSWORD_HASH`: a Werkzeug hash
such as `scrypt:32768:8:1$salt$hash` is silently truncated to
`scrypt:32768:8:1` when unescaped, because Compose expands `$salt` and `$hash`
as undefined variables, and web then refuses to start. The hash stays an
environment value rather than a mounted secret: moving it to a secret file
would add a third secret and a config path for no security gain, since it is a
hash rather than a credential, so the escaping rule is the contract.

Start with `scripts/v2_compose_up.sh`. It verifies V1/V2 host-artifact roots
are disjoint, rebuilds the `migrate` image, starts PostgreSQL, waits for its
health check, runs the explicit one-shot Alembic migration, verifies that the
database revision now equals the head resolved from that image's migration
graph, and only then starts web, scheduler, and the single worker. Normal roles
never migrate on startup.

The explicit `migrate` rebuild is required, not cosmetic. That service sits
behind the `manual` Compose profile, so a plain `docker compose build` skips
it; a stale migrate image would run `alembic upgrade head` against its own
older graph, exit 0, and leave the database behind the checked-out code while
`/readyz` fails. The post-migration revision check fails the deployment loudly
if that ever happens again, and no revision name is hardcoded anywhere. `/healthz` is liveness-only;
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
seven days. It never accesses V1 data. Archives are created mode 600 through
the script's umask rather than tightened afterwards, and the script fails if an
archive ends up with any other mode.

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
