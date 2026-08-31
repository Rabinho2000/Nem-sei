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
# The archives the timer writes are owned by root, so verifying one means
# running this as root, where git refuses a repository owned by somebody else.
root=${NEMSEI_V2_REPO_ROOT:-$(git rev-parse --show-toplevel)}
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
revision_output=$("${compose[@]}" exec -T postgres psql -U nemsei -d "$restore_db" -At \
  -c 'SELECT version_num FROM alembic_version')
mapfile -t revisions < <(printf '%s\n' "$revision_output" | sed '/^[[:space:]]*$/d')
if (( ${#revisions[@]} != 1 )); then
  echo "Restored database must contain exactly one Alembic revision; found ${#revisions[@]}." >&2
  exit 1
fi
revision=${revisions[0]}

if ! head_output=$("${compose[@]}" run --rm --no-deps -T \
  -v "$root/scripts/v2_resolve_alembic_head.py:/app/v2_resolve_alembic_head.py:ro" \
  -v "$root/alembic.ini:/app/alembic.ini:ro" \
  -v "$root/migrations:/app/migrations:ro" \
  migrate python /app/v2_resolve_alembic_head.py --config /app/alembic.ini); then
  echo "Unable to resolve the current repository Alembic head." >&2
  exit 1
fi
expected_head=$(printf '%s\n' "$head_output" | awk -F= '/^__NEMSEI_ALEMBIC_HEAD__=/{print $2}' | tail -n 1)
if [[ -z $expected_head ]]; then
  echo "Unable to resolve the current repository Alembic head." >&2
  exit 1
fi
if [[ $revision != "$expected_head" ]]; then
  echo "Restored Alembic revision '$revision' does not match repository head '$expected_head'." >&2
  exit 1
fi
"${compose[@]}" exec -T postgres psql -U nemsei -d "$restore_db" -v ON_ERROR_STOP=1 \
  -c 'SELECT COUNT(*) AS restored_jobs FROM jobs'
printf 'V2 PostgreSQL restore smoke test passed: %s\n' "$restore_db"
