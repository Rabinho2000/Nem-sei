# Nem-sei V1 known limitations

This document records the final V1 operating constraints; it does not change
V1 behaviour.

- Flask, APScheduler, and background work are composed through the large
  `monitoring_board.app_factory` module.
- APScheduler runs inside the Flask/Gunicorn application process. Production
  must remain at one Gunicorn worker and one application instance per SQLite
  database.
- SQLite schema changes are idempotent application code rather than versioned
  migrations.
- SQLite permits one writer at a time. Long requests, background writes, and
  manual database operations can contend for locks.
- Provider APIs remain external assumptions: credentials, response shapes,
  units, pagination, rate limits, token refresh, and partial failures require
  operational review.
- Reporting and generated artifacts depend on both SQLite rows and files under
  the runtime uploads directory; both belong in the same backup set.
- Existing background-job recovery and reporting controls are V1-specific and
  are not a reusable V2 queue contract.
- V1 uses a configuration-backed administrator login rather than persisted
  users or role-based access control.

Future V2 work must preserve the documented data-quality rules without moving
or refactoring V1 code.
