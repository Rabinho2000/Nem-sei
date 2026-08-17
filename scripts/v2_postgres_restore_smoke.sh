#!/usr/bin/env bash
# Restore one V2 logical backup into a disposable PostgreSQL database and verify it.
set -euo pipefail

if (( $# != 1 )); then
  echo "Usage: $0 /absolute/path/to/nemsei-v2-<timestamp>.dump" >&2
  exit 2
fi

archive=$1
if [[ ! -s $archive ]]; then
  echo "Backup archive must exist and be non-empty." >&2
  exit 2
fi
root=$(git rev-parse --show-toplevel)
env_file=${NEMSEI_V2_ENV_FILE:-$root/.env.v2}
project=${NEMSEI_V2_COMPOSE_PROJECT:-nemsei-v2}
restore_db="nemsei_v2_restore_$(date -u +%Y%m%d%H%M%S)"
compose=(docker compose --project-name "$project" --env-file "$env_file" -f "$root/docker-compose.v2.yml")

cleanup() {
  "${compose[@]}" exec -T postgres psql -U nemsei -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $restore_db" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" exec -T postgres psql -U nemsei -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE $restore_db"
"${compose[@]}" exec -T postgres pg_restore -U nemsei -d "$restore_db" --exit-on-error < "$archive"
revision=$("${compose[@]}" exec -T postgres psql -U nemsei -d "$restore_db" -At \
  -c 'SELECT version_num FROM alembic_version')
if [[ $revision != 0004_asset_import_hardening ]]; then
  echo "Unexpected restored Alembic revision: $revision" >&2
  exit 1
fi
"${compose[@]}" exec -T postgres psql -U nemsei -d "$restore_db" -v ON_ERROR_STOP=1 \
  -c 'SELECT COUNT(*) AS restored_jobs FROM jobs'
printf 'V2 PostgreSQL restore smoke test passed: %s\n' "$restore_db"
