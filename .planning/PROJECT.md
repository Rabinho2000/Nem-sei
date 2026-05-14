# Project: PV O&M Monitoring Board

## Vision

Maintain and evolve a practical Flask monitoring board for PV O&M operations. The app should help a single operator track installations, daily monitoring status, tickets, contracts, production performance, provider integrations, Telegram alerts, exports, and field routes without adding heavy infrastructure.

## Current Product

The existing application is a server-rendered Flask app backed by SQLite. It runs locally or in Docker on a Raspberry Pi 5, with Cloudflare Tunnel as the intended remote access path. The app already includes authentication, CSRF protection, Excel import/export, PDF report generation, FusionSolar and Sigenergy integration paths, Telegram alerts, background jobs through APScheduler, and field route planning.

## Deployment Constraints

- Keep deployment simple: Docker Compose, one Gunicorn worker, SQLite, local filesystem persistence.
- Preserve Raspberry Pi compatibility and avoid memory-heavy services.
- Do not add Celery, Redis, Postgres, Kubernetes, or cloud infrastructure.
- Treat `.env`, SQLite databases, WAL/SHM files, uploads, logs, reports, Excel files, PDFs, and backups as runtime artifacts that must stay out of Git.
- Keep APScheduler single-process assumptions explicit.

## Product Priorities

- Reliability of daily monitoring, integrations, alerts, and reports.
- Safe SQLite behavior under request, scheduler, and background-job writes.
- Clear operational behavior on Raspberry Pi deployment.
- Better confidence in provider parsing, timestamps, units, and rate-limit handling.
- Incremental maintainability improvements around high-risk areas, especially `app.py`, without unrelated rewrites.

## Out Of Scope

- Replacing SQLite with a server database.
- Moving background work to Celery, Redis, or a separate queue system.
- Building a multi-tenant or role-based enterprise platform.
- Public internet exposure without Cloudflare Access or equivalent protection.
- Large framework rewrites or frontend redesigns unrelated to current operational needs.

