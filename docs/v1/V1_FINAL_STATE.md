# Nem-sei V1 final state

V1 was frozen on 2026-08-13 from the `main` branch. The annotated tag
`v1-final-2026-08-13` identifies the exact source revision approved for V1
operation and rollback.

## Frozen application boundary

The V1 runtime is the root `app.py`, the `monitoring_board/` package,
`templates/`, `static/`, the existing Docker and Compose files, V1 environment
examples, `requirements.txt`, `tests/`, operational scripts, and the existing
GitHub CI workflow.

These paths are read-only after this freeze except for a separately approved
V1 hotfix. V2 must be developed in its own worktree and must not import or
reuse V1 Python modules, templates, database files, or runtime directories.

## Verified release checks

The freeze commit is accepted only after these checks pass from the V1
worktree:

```text
python -m pytest -q tests
python -m ruff check monitoring_board tests
python -m compileall monitoring_board
python -m pip check
```

## Supported V1 entrypoints

- Local development: `python app.py`
- Container web runtime: `gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app`
- Production Compose: `docker-compose.yml`
- Preview Compose: `docker-compose.preview.yml`

V1 retains its existing single-process scheduler model and is not altered by
the V2 foundation work.
