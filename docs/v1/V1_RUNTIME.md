# Nem-sei V1 runtime

## Runtime paths and network

V1 uses `DATA_DIR` for SQLite, uploads, logs, and backups. In the production
Compose deployment, `./data` mounts at `/data` and web is bound to
`127.0.0.1:5000`. Preview uses its own Compose file and gateway on port 5002.

V1 runtime artifacts, environment files, databases, logs, uploads, reports,
and backups are intentionally excluded from Git.

## Scheduler and database operation

The V1 scheduler is embedded in the Flask process and therefore requires one
Gunicorn worker. It registers provider sync, reporting, rate-limit recovery,
and notification jobs according to configuration. Do not scale V1 web
processes against the same SQLite file.

SQLite runs with foreign keys, WAL, normal synchronous mode, and a busy
timeout. Backups must include SQLite and required upload artifacts together.

## V1/V2 separation

V2 must use a separately created Git worktree and a physically separate host
data root. A V2 container may use `/data` internally, but that mount must not
refer to the V1 host data directory or any nested path.
