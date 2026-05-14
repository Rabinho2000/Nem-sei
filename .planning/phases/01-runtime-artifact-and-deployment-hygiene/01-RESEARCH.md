# Phase 1: Runtime Artifact And Deployment Hygiene - Research

**Researched:** 2026-05-14
**Domain:** Flask/Gunicorn deployment hygiene, SQLite WAL artifacts, Docker runtime paths
**Confidence:** HIGH

## Summary

This phase should be planned as a small hygiene/documentation pass, not an application behavior change. The repository already uses the intended stack shape: Flask, SQLite in WAL mode, APScheduler in-process, Docker Compose with `DATA_DIR=/data`, and Gunicorn with exactly one worker and four threads. The main implementation gap is that `.gitignore` and `.dockerignore` ignore `*.db` but not SQLite WAL sidecars (`*.db-wal`, `*.db-shm`), and the current worktree confirms `monitoring_board.db-wal` and `monitoring_board.db-shm` appear as untracked files.

Official SQLite docs confirm that WAL mode creates extra `-wal` and `-shm` files beside the main database, that the WAL file is part of database persistent state while in use, and that these files can remain on disk after non-clean shutdowns. Official Gunicorn docs confirm that `--workers` controls worker processes; because this app starts APScheduler inside the Flask process, the project rule should remain "one Gunicorn worker only unless scheduling is redesigned."

**Primary recommendation:** Plan one narrow task that updates ignore files and deployment documentation, then verifies `DATA_DIR=/data`, `gunicorn -w 1`, and sidecar ignore behavior with simple commands; only add tests if runtime path code changes.

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-001 | Preserve the current Flask, SQLite, APScheduler, Docker Compose, and Raspberry Pi deployment shape. | Existing `Dockerfile`, `docker-compose.yml`, `README.md`, and `docs/raspberry-pi-deployment.md` already preserve Flask/Gunicorn/Docker/Raspberry Pi; planner should avoid new infrastructure. |
| REQ-003 | Prevent runtime artifacts and sensitive local files from being committed, including SQLite WAL/SHM files. | `.gitignore` and `.dockerignore` need `*.db-wal`, `*.db-shm`, and probably equivalent SQLite sidecar variants; current `git status` shows WAL/SHM files as untracked. |
| REQ-004 | Keep scheduled jobs registered once per process and preserve the documented one-worker deployment requirement. | `app.py` starts a process-local `BackgroundScheduler`; Gunicorn workers are separate worker processes, so docs/commands must remain `-w 1`. |
| REQ-010 | Strengthen backup, restore, and deployment documentation for Raspberry Pi operation. | README and Raspberry Pi guide already include backup/restore and one-worker guidance; planner should tighten warnings against multiple app instances/processes sharing the same `./data`. |

</phase_requirements>

## Standard Stack

### Core

| Component | Version | Purpose | Why Standard |
|-----------|---------|---------|--------------|
| Flask | `>=3.1.3` | Server-rendered monitoring board | Existing app framework; no change needed. |
| SQLite | Python stdlib `sqlite3` | Local persistence with WAL mode | Existing persistence layer; appropriate for Raspberry Pi single-node deployment. |
| APScheduler | `>=3.11.0` | In-process scheduled syncs/background jobs | Existing scheduler; must stay single-process aware. |
| Gunicorn | `>=23.0.0` | Production WSGI server | Existing Docker command; `--workers 1 --threads 4` matches APScheduler constraint. |
| Docker Compose | Host-installed | Raspberry Pi deployment wrapper | Existing simple deployment model with `./data:/data`. |

### Supporting

| Tool/File | Version | Purpose | When to Use |
|-----------|---------|---------|-------------|
| `.gitignore` | n/a | Prevent local artifacts from appearing in Git status | Update for SQLite sidecars and other runtime output. |
| `.dockerignore` | n/a | Prevent local artifacts from being copied into image build context | Mirror runtime artifact exclusions from `.gitignore`. |
| `scripts/backup.sh` | n/a | Consistent SQLite backup using `sqlite3 ".backup"` | Keep documented for WAL-mode backups. |
| `tests/test_runtime_paths.py` | pytest | Runtime path behavior tests | Update only if `monitoring_board/runtime.py` behavior changes. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| In-process APScheduler | Celery/RQ/external worker | Out of scope by REQ-001/REQ-018 and project instructions. |
| SQLite file deployment | Postgres or managed DB | Out of scope and unnecessary for this hygiene phase. |
| Manual artifact cleanup | Ignore patterns | Ignore patterns are lower risk and enforce hygiene continuously. |
| Multi-worker Gunicorn | `-w 2+` | Would duplicate process-local scheduler jobs unless scheduler leadership/locking is redesigned. |

**Installation:** No package installation required for this phase.

## Architecture Patterns

### Recommended Project Structure

```text
.
├── .gitignore                         # Git runtime artifact exclusions
├── .dockerignore                      # Docker build context exclusions
├── Dockerfile                         # One-worker Gunicorn default command
├── docker-compose.yml                 # DATA_DIR=/data and ./data:/data
├── README.md                          # Local + Docker/Raspberry Pi operations
├── docs/raspberry-pi-deployment.md    # Production deployment guide
├── monitoring_board/runtime.py        # Runtime path resolution
└── tests/test_runtime_paths.py        # Existing path behavior coverage
```

### Pattern 1: Ignore SQLite WAL Sidecars Explicitly

**What:** Add ignore patterns for SQLite WAL and SHM files beside existing `*.db`, `*.sqlite`, and `*.sqlite3` patterns.
**When to use:** Always for this project because `configure_database_for_runtime()` enables WAL mode and local development writes runtime DB files in the repo root by default.
**Example:**

```gitignore
# Local database and SQLite sidecars
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.sqlite3
*.sqlite3-wal
*.sqlite3-shm
```

Planner note: consider `*.db-journal`, `*.sqlite-journal`, and `*.sqlite3-journal` as conservative rollback-journal coverage, but the phase requirement specifically calls out WAL/SHM.

### Pattern 2: Keep Runtime State Under DATA_DIR In Docker

**What:** Docker Compose should keep `DATA_DIR=/data` and mount host `./data` to container `/data`.
**When to use:** All Raspberry Pi deployment docs and examples.
**Example:**

```yaml
environment:
  DATA_DIR: /data
volumes:
  - ./data:/data
command: gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app
```

Current code support:

```python
def build_runtime_paths(data_dir_value: str | None = None) -> RuntimePaths:
    data_dir = resolve_data_dir(data_dir_value)
    uploads_dir = data_dir / "uploads"
    return RuntimePaths(
        data_dir=data_dir,
        database=data_dir / "monitoring_board.db",
        backups=data_dir / "backups",
        uploads=uploads_dir,
        contracts=uploads_dir / "contracts",
        logs=data_dir / "logs",
    )
```

### Pattern 3: Document One Worker As A Correctness Constraint

**What:** Keep `gunicorn -w 1 --threads 4` and explicitly warn that multiple worker processes or multiple app instances against the same data directory can duplicate scheduled jobs.
**When to use:** Dockerfile, Compose command comments/docs, README, Raspberry Pi deployment guide, troubleshooting.
**Example:**

```text
Run exactly one Gunicorn worker while APScheduler is in-process.
Do not set `--workers` above 1, run `docker compose up --scale`, or start a
second app instance against the same `./data` unless scheduling is redesigned.
Threads are acceptable for request concurrency because they stay in one worker
process.
```

### Anti-Patterns to Avoid

- **Changing scheduler architecture in this phase:** This phase is hygiene/docs only; redesigning scheduler leadership belongs in a later phase.
- **Adding external services:** Celery, Redis, Postgres, RQ, Kubernetes, or cloud backup infrastructure violates project constraints.
- **Editing runtime path semantics casually:** `DATA_DIR` is already covered by tests; only touch `monitoring_board/runtime.py` if verification finds a real inconsistency.
- **Only updating `.gitignore`:** Docker build context should also exclude sidecars so local DB state is not sent to Docker builds.
- **Treating WAL/SHM as source:** SQLite sidecars are runtime database files and may contain committed state while active; they should not be committed or copied into images.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Runtime artifact prevention | Custom cleanup script for Git status | `.gitignore` and `.dockerignore` patterns | Native, simple, continuous prevention. |
| WAL-mode backup consistency | Raw `cp monitoring_board.db` while app is active | Existing `scripts/backup.sh` with `sqlite3 ".backup"` | SQLite WAL state can live outside the main DB file while active. |
| Multi-process scheduler safety | Ad hoc file locks in this phase | Keep one worker/process documented | Proper scheduler leadership/locking is larger architecture work. |
| Runtime path validation | New config framework | Existing `build_runtime_paths()` and pytest coverage | Existing code is small, tested, and aligned with deployment docs. |

**Key insight:** The right plan is to enforce existing deployment assumptions, not to make the app magically safe under multiple processes.

## Common Pitfalls

### Pitfall 1: Ignoring Only `*.db`

**What goes wrong:** `monitoring_board.db-wal` and `monitoring_board.db-shm` still appear as untracked files.
**Why it happens:** SQLite appends `-wal` and `-shm` to the full database filename, so `*.db` does not match `*.db-wal`.
**How to avoid:** Add explicit sidecar patterns for `.db`, `.sqlite`, and `.sqlite3` names in `.gitignore` and `.dockerignore`.
**Warning signs:** `git status --short --untracked-files=all` shows `?? monitoring_board.db-wal` or `?? monitoring_board.db-shm`.

### Pitfall 2: Multi-Worker Gunicorn Duplicates Scheduler State

**What goes wrong:** Each worker process imports `app.py`, creates its own Flask app, and starts a process-local scheduler, causing duplicate scheduled syncs or Telegram summaries.
**Why it happens:** Gunicorn workers are independent processes; `SCHEDULER` is a module global only within one process.
**How to avoid:** Keep `--workers 1`; document that increasing workers/processes requires scheduler redesign.
**Warning signs:** Docker command uses `-w 2+`, `--workers 2+`, `WEB_CONCURRENCY` is introduced, or Compose scaling is suggested.

### Pitfall 3: Runtime Data Leaks Into Docker Image Context

**What goes wrong:** Local databases, sidecars, uploads, logs, or generated reports get sent to Docker build context and may be copied into the image.
**Why it happens:** `.dockerignore` misses runtime artifacts even if `.gitignore` catches them.
**How to avoid:** Keep `.dockerignore` aligned with `.gitignore` for local/runtime artifacts.
**Warning signs:** `docker build` context is unexpectedly large or local runtime files exist beside app code.

### Pitfall 4: Documentation Mentions `DATA_DIR` But Examples Drift

**What goes wrong:** Operators restore/backup one path while the app writes to another.
**Why it happens:** Host paths (`./data`) and container paths (`/data`) are easy to mix up.
**How to avoid:** Keep docs explicit: host `./data` is mounted to container `/data`; Compose sets `DATA_DIR=/data`; backup commands on host use `DATA_DIR=./data`.
**Warning signs:** Docs suggest `DATA_DIR=./data` inside Docker or `/data` for host-side backup commands outside the container.

## Code Examples

Verified patterns from project and official sources:

### Runtime Path Resolution

```python
# Source: monitoring_board/runtime.py
def resolve_data_dir(data_dir_value: str | None = None) -> Path:
    raw_value = os.environ.get("DATA_DIR", "") if data_dir_value is None else data_dir_value
    if not raw_value or not raw_value.strip():
        return BASE_DIR

    configured_path = Path(raw_value.strip()).expanduser()
    if not configured_path.is_absolute():
        configured_path = BASE_DIR / configured_path
    return configured_path.resolve()
```

### One-Worker Docker Command

```dockerfile
# Source: Dockerfile
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]
```

### Compose Runtime Data Mount

```yaml
# Source: docker-compose.yml
environment:
  DATA_DIR: /data
  SESSION_COOKIE_SECURE: "true"
volumes:
  - ./data:/data
command: gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app
```

### Verification Commands For Executor

```powershell
git check-ignore -v monitoring_board.db monitoring_board.db-wal monitoring_board.db-shm
git status --short --untracked-files=all
python -m pytest -q tests/test_runtime_paths.py
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Ignoring only the main SQLite DB file | Ignore main DB plus WAL/SHM sidecars | Required now by WAL mode usage | Prevents runtime sidecars from appearing as Git candidates. |
| Copying only `monitoring_board.db` for backup | Use SQLite `.backup` command | Already implemented in `scripts/backup.sh` | Safer for active WAL-mode databases. |
| Scaling Gunicorn workers for concurrency | One worker plus threads | Existing project constraint | Avoids duplicate in-process schedulers. |
| Runtime state in project root for production | `DATA_DIR=/data` mounted from host `./data` | Existing Docker pattern | Keeps production data persistent and outside image filesystem. |

**Deprecated/outdated:**
- Treating a WAL-mode SQLite database as only one file: official SQLite docs state WAL mode uses associated `-wal` and `-shm` files.
- Increasing Gunicorn worker count as routine tuning: not valid for this app while scheduled jobs run in-process.
- File-copy-only database backups while app is active: use `.backup` instead.

## Open Questions

1. **Should `reports/` also be added to ignore files in this phase?**
   - What we know: Project instructions say generated reports/PDFs are runtime artifacts; root has a `reports/` directory, and `.gitignore` currently ignores `*.pdf` but not `reports/`.
   - What's unclear: The phase scope names SQLite sidecars specifically, not every runtime directory.
   - Recommendation: Include `reports/` if present and clearly generated; keep the commit scoped and mention it as runtime artifact hygiene.

2. **Should docs include a hard warning about `docker compose up --scale monitoring-board=2`?**
   - What we know: Current docs warn against multiple Gunicorn workers and a second app instance sharing `./data`.
   - What's unclear: README does not explicitly mention Compose scaling.
   - Recommendation: Add one sentence in deployment docs; it is documentation-only and directly supports REQ-004.

3. **Should this phase add tests?**
   - What we know: Existing `tests/test_runtime_paths.py` covers `DATA_DIR` behavior.
   - What's unclear: No runtime code change is expected.
   - Recommendation: Do not add tests for ignore/docs-only changes; run existing path tests as verification. Add/update tests only if `runtime.py`, Docker path semantics, or scheduler startup behavior changes.

## Sources

### Primary (HIGH confidence)

- Project files: `.gitignore`, `.dockerignore`, `Dockerfile`, `docker-compose.yml`, `README.md`, `docs/raspberry-pi-deployment.md`, `monitoring_board/runtime.py`, `app.py`, `tests/test_runtime_paths.py`.
- SQLite WAL docs: https://www.sqlite.org/wal.html - verified WAL creates `-wal` and `-shm`, WAL is part of persistent state while active, and safe removal/copying concerns.
- SQLite WAL file format docs: https://www.sqlite.org/walformat.html - verified sidecar names and lifecycle behavior.
- Gunicorn settings docs: https://gunicorn.org/reference/settings/ - verified `--workers` controls worker process count and `--threads` controls threads.
- Gunicorn design docs: https://docs.gunicorn.org/en/stable/design.html - verified worker/thread model.
- APScheduler user guide: https://apscheduler.readthedocs.io/en/stable/userguide.html - verified `BackgroundScheduler` runs in the application process/thread context after `start()`.

### Secondary (MEDIUM confidence)

- Current repository `git status --short --untracked-files=all` - verified current untracked `monitoring_board.db-wal` and `monitoring_board.db-shm`; status also shows many unrelated user changes that implementation must not revert.

### Tertiary (LOW confidence)

- None needed. The phase is covered by project files and official docs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Directly from `requirements.txt`, Docker files, and codebase map.
- Architecture: HIGH - Runtime path and scheduler startup code are simple and locally verified.
- Pitfalls: HIGH - SQLite and Gunicorn claims verified against official docs; current worktree demonstrates the sidecar ignore gap.
- Testing guidance: HIGH - Existing runtime path tests cover `DATA_DIR`; phase is expected to be ignore/docs-only.

**Research date:** 2026-05-14
**Valid until:** 2026-06-13 for docs/ignore/runtime-path planning; re-check official docs if changing scheduler architecture or deployment model.
