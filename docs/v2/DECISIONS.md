# Nem-sei V2 decisions

- Flask and direct SQLAlchemy are retained; Flask-SQLAlchemy is not used.
- SQLite is the initial database with WAL, short transactions, and one worker.
- Alembic is the only schema migration mechanism; web does not auto-migrate.
- Runtime data and Git worktrees are isolated from V1.
- External capabilities default to deny.
- Jobs are at-least-once and all future side effects require explicit
  idempotency strategies.
