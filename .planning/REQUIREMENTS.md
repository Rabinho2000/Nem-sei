# Requirements

## Must Have

- REQ-001: Preserve the current Flask, SQLite, APScheduler, Docker Compose, and Raspberry Pi deployment shape.
- REQ-002: Keep all existing monitoring, asset, ticket, contract, integration, export, report, alert, and field-route workflows working unless a phase explicitly changes them.
- REQ-003: Prevent runtime artifacts and sensitive local files from being committed, including SQLite WAL/SHM files.
- REQ-004: Keep scheduled jobs registered once per process and preserve the documented one-worker deployment requirement.
- REQ-005: Add focused tests for changes that affect database writes, scheduling, provider parsing, calculations, imports, exports, reports, alerts, or auth/security.
- REQ-006: Treat FusionSolar and Sigenergy API fields, pagination, rate limits, timestamps, and units as assumptions that must be verified before expanding behavior.
- REQ-007: Keep credential handling production-safe: prefer environment variables for provider secrets and document that SQLite backups can contain secrets.

## Should Have

- REQ-008: Reduce risk in the largest modules by extracting cohesive helpers only when touching nearby behavior.
- REQ-009: Improve observability of background jobs, sync runs, and failed scheduled work without adding new services.
- REQ-010: Strengthen backup, restore, and deployment documentation for Raspberry Pi operation.
- REQ-011: Add regression tests around date/time, production calculations, report periods, and alert throttling.
- REQ-012: Make import/export edge cases safer, especially Excel schema drift and PDF/report generation failures.

## Nice To Have

- REQ-013: Add lightweight health checks for local operational status.
- REQ-014: Improve field route planning ergonomics and error handling for slow or failed map/geocoding APIs.
- REQ-015: Gradually split `app.py` into smaller modules when the extraction is directly tied to a tested behavior change.

## Out Of Scope

- REQ-016: Multi-user role management beyond the existing authenticated admin model.
- REQ-017: Replacing the server-rendered UI with a SPA.
- REQ-018: Introducing external queue, cache, or database infrastructure.

