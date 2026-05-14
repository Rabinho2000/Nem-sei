---
phase: 01-runtime-artifact-and-deployment-hygiene
status: passed
verified_at: 2026-05-14
verifier: codex
---

# Phase 01 Verification

## Result

Phase 01 achieved its goal: local/runtime SQLite artifacts are excluded from Git and Docker build context, and Raspberry Pi deployment assumptions are explicit without requiring runtime code, Docker command, or Compose behavior changes in this phase.

## Checked Must-Haves

### Git ignores SQLite database files and sidecars

Status: passed

Evidence:

- `.gitignore` contains database and sidecar patterns for `.db`, `.sqlite`, and `.sqlite3`: `*.db`, `*.db-wal`, `*.db-shm`, `*.db-journal`, `*.sqlite`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite-journal`, `*.sqlite3`, `*.sqlite3-wal`, `*.sqlite3-shm`, `*.sqlite3-journal`.
- `git check-ignore -v monitoring_board.db monitoring_board.db-wal monitoring_board.db-shm monitoring_board.db-journal monitoring_board.sqlite monitoring_board.sqlite-wal monitoring_board.sqlite-shm monitoring_board.sqlite-journal monitoring_board.sqlite3 monitoring_board.sqlite3-wal monitoring_board.sqlite3-shm monitoring_board.sqlite3-journal` returned matching `.gitignore` rules for every checked file.

### Docker build context excludes the same sidecars

Status: passed

Evidence:

- `.dockerignore` contains matching database and sidecar patterns for `.db`, `.sqlite`, and `.sqlite3`: `*.db`, `*.db-wal`, `*.db-shm`, `*.db-journal`, `*.sqlite`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite-journal`, `*.sqlite3`, `*.sqlite3-wal`, `*.sqlite3-shm`, `*.sqlite3-journal`.
- `Select-String -Path .dockerignore` confirmed the patterns on `.dockerignore` lines 9-20.

### README and Raspberry Pi docs warn about in-process APScheduler

Status: passed

Evidence:

- `README.md` documents `gunicorn -w 1 --threads 4`, states APScheduler runs inside the app process, and warns not to increase workers, use `WEB_CONCURRENCY`, run `docker compose up --scale monitoring-board=2`, or start a second app instance against the same `./data`.
- `docs/raspberry-pi-deployment.md` contains the same one-worker warning, says threads are acceptable inside one worker process, and repeats the no-scale/no-second-instance constraints in the startup, database-locked troubleshooting, and what-not-to-do sections.
- `Select-String -Path README.md,docs/raspberry-pi-deployment.md -Pattern 'APScheduler','Gunicorn','worker','--scale','DATA_DIR','/data','./data','db-wal','db-shm'` returned the expected documentation hits.

### DATA_DIR host/container path language is consistent

Status: passed

Evidence:

- `docker-compose.yml` sets `DATA_DIR: /data` and mounts `./data:/data`.
- `README.md` states persistent data is in host `./data`, mounted as container `/data`, with Compose `DATA_DIR=/data`; host backup commands use `DATA_DIR=./data`.
- `docs/raspberry-pi-deployment.md` states runtime data is stored in host `./data`, mounted into the container as `/data`; Compose sets `DATA_DIR=/data`; host backup and restore commands use `DATA_DIR=./data`.
- `Select-String -Path Dockerfile,docker-compose.yml -Pattern 'gunicorn.*-w 1','DATA_DIR: /data','./data:/data'` confirmed the Compose values. `Dockerfile` also has `CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]`.

### No runtime code or deployment behavior changes were required for this phase

Status: passed

Evidence:

- Phase commits in history are `2b8d717 chore(01-01): ignore SQLite runtime sidecars`, `d162c4a docs(01-01): document one-worker deployment constraints`, and `eb1eb13 docs(01-01): complete runtime hygiene plan`.
- `git diff --name-only HEAD~2..HEAD -- Dockerfile docker-compose.yml app.py monitoring_board/runtime.py` returned no files, confirming the implementation commits did not change runtime code, Docker command behavior, or Compose behavior.
- Current working tree has an unrelated `app.py` diff, but it is not part of the Phase 01 commits being verified.

## Additional Verification

- `python -m pytest -q tests/test_runtime_paths.py` passed: 4 tests.
