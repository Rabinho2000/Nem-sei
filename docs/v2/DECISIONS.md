# Nem-sei V2 decisions

- Flask and direct SQLAlchemy are retained; Flask-SQLAlchemy is not used.
- SQLite is the initial database with WAL, short transactions, and one worker.
- Alembic is the only schema migration mechanism; web does not auto-migrate.
- Runtime data and Git worktrees are isolated from V1.
- External capabilities default to deny.
- Jobs are at-least-once and all future side effects require explicit
  idempotency strategies.

## Future side-effect idempotency contracts

Every side-effecting handler must persist an idempotency key before invoking an
external system, define replay behavior, and record an uncertain external
outcome for reconciliation rather than blindly repeating the call.

| Future operation | Idempotency key | Replay behavior | Uncertain external outcome |
| --- | --- | --- | --- |
| Production/history import | provider, external reference, observed period, fact version | Durable natural-key upsert | Re-fetch and reconcile by external reference/period |
| Report generation | approved snapshot or artifact specification/version | Return the existing immutable artifact | Retain pending artifact state and regenerate only from the same snapshot |
| Report distribution | snapshot, artifact, recipient, channel, template version | Return the existing delivery record | Query provider delivery status where available; otherwise mark for manual reconciliation |
| Notification | business event, destination, template version | Reuse the durable delivery record | Mark delivery uncertain; never silently resend |
| Provider mutation | persisted operation key and provider idempotency key | Return recorded provider outcome | Query provider operation/status before a retry; require manual reconciliation if unavailable |
