# Reporting Operations

This page covers the configurable reporting system after the roadmap hardening
phase. It is intentionally operational: keep architecture notes in
`docs/reporting_architecture.md`.

## What Is Guarded

- Template scope is checked on duplicate, default selection, edit, preview and
  generation.
- Portfolio templates cannot be used for another portfolio.
- Client-scoped templates require the resolved backend client key to match.
- Snapshot generation is locked to the snapshot portfolio and period; submitted
  period overrides are rejected.
- Report downloads verify the stored path, file size and SHA-256 before serving.
- Generated files are written through a per-run `.staging` directory and moved
  into place only after hash verification.
- Logo uploads accept only bounded PNG/JPEG files with valid signatures and
  dimensions.

## Storage Reconciliation

Run a dry reconciliation from the project root:

```powershell
python -m monitoring_board.reporting_storage_check --dry-run
```

Use an explicit database when running outside the normal app environment:

```powershell
python -m monitoring_board.reporting_storage_check --database .\data\monitoring_board.db --dry-run
```

For isolated checks, pass both database and generated-report root:

```powershell
python -m monitoring_board.reporting_storage_check --database .\data\monitoring_board.db --root .\data\uploads\generated_reports --dry-run
```

The command reports findings as tab-separated rows:

- `ok`: database row matches the stored file.
- `missing_file`: database row points to a missing file.
- `size_mismatch`: stored size differs from the database row.
- `hash_mismatch`: SHA-256 differs from the database row.
- `invalid_path`: stored relative path is outside the reporting output root.
- `unexpected_symlink`: stored path is a symlink.
- `orphan_file`: file exists on disk without a database row.
- `stale_staging`: old file remains in a staging directory.

Cleanup is deliberately conservative. It only removes orphan files and stale
staging files:

```powershell
python -m monitoring_board.reporting_storage_check --database .\data\monitoring_board.db --cleanup
```

## Health Endpoint

Authenticated users can check reporting health at:

```text
/reporting-health
```

The JSON response includes database counts, default-template coverage, storage
findings and stale running jobs. It does not delete files.

## Release Checklist

Before merging reporting changes:

```powershell
python -m pytest -q
python -m ruff check monitoring_board tests
python -m compileall monitoring_board
python -m pip check
python -c "from monitoring_board.reporting import templates, portfolio, invoices; print('reporting imports ok')"
python -c "from monitoring_board.services import report_rendering, portfolio_reporting, invoice_extraction; print('service imports ok')"
git diff --check
```

For a production deployment:

- Confirm `/reporting-health` has no `missing_file`, `hash_mismatch`,
  `size_mismatch`, `invalid_path` or `unexpected_symlink` findings.
- Run the storage check in `--dry-run` mode first.
- Keep `DATA_DIR/uploads/report_outputs/` and the SQLite database in the same
  backup set.
- Do not run cleanup until a dry run has been reviewed.
