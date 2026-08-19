#!/usr/bin/env bash
# Canonical production/preview V2 startup. Direct `docker compose up` is unsupported.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
mode=${NEMSEI_V2_DEPLOYMENT_MODE:-production}
v1_root=${NEMSEI_V1_DATA_ROOT:-/opt/server/apps/Nem-sei/data}
v2_root=${NEMSEI_V2_HOST_DATA_ROOT:-/opt/server/apps/Nem-sei-v2-data}
env_file=${NEMSEI_V2_ENV_FILE:-$root/.env.v2}

if (( $# != 0 )); then
  echo "This wrapper starts exactly one worker and accepts no Compose scale arguments." >&2
  exit 2
fi
if [[ $mode != production && $mode != preview && $mode != development ]]; then
  echo "NEMSEI_V2_DEPLOYMENT_MODE must be production, preview, or development." >&2
  exit 2
fi
if [[ $mode != development && $v2_root != /opt/server/apps/Nem-sei-v2-data ]]; then
  echo "Production/preview V2 data root is fixed at /opt/server/apps/Nem-sei-v2-data." >&2
  exit 2
fi
if [[ ${NEMSEI_V2_WORKER_SCALE:-1} != 1 || ${NEMSEI_V2_WORKER_CONCURRENCY:-1} != 1 ]]; then
  echo "V2 deploys exactly one worker at this checkpoint; scaling is not yet enabled." >&2
  exit 2
fi

mkdir -p "$v2_root"
python3 "$root/scripts/verify_v2_runtime_isolation.py" --v1-data-root "$v1_root" --v2-data-root "$v2_root" --compose-file "$root/docker-compose.v2.yml"
v2_real=$(realpath -m "$v2_root")

compose=(docker compose --project-name nemsei-v2 --env-file "$env_file" -f "$root/docker-compose.v2.yml")
rendered=$("${compose[@]}" config --format json)
RENDERED_COMPOSE="$rendered" python3 - "$v2_real" <<'PY'
import json
import os
import sys

expected = os.path.realpath(sys.argv[1])
config = json.loads(os.environ["RENDERED_COMPOSE"])
if config["services"]["postgres"].get("ports"):
    raise SystemExit("PostgreSQL must not publish a host port")
PY

# Build every image this deployment is about to run, including the `migrate`
# service that sits behind the `manual` profile and that a plain
# `docker compose build` silently skips. A stale migrate image runs
# `alembic upgrade head` against its own older migration graph, exits 0, and
# leaves the database behind the checked-out code; a stale web image carries an
# older graph than the migrated database and fails its readiness check.
"${compose[@]}" --profile manual build
"${compose[@]}" up -d postgres
"${compose[@]}" run --rm migrate

# Prove the migration actually reached the checked-out head. The head is always
# resolved from the freshly built image's migration graph; no revision name is
# ever hardcoded here.
live_revision=$("${compose[@]}" exec -T postgres psql -U nemsei -d nemsei_v2 -At \
  -c 'SELECT version_num FROM alembic_version' | tr -d '\r' | sed '/^[[:space:]]*$/d' | tail -n 1)
if ! "${compose[@]}" run --rm --no-deps -T \
  -v "$root/scripts/v2_resolve_alembic_head.py:/app/v2_resolve_alembic_head.py:ro" \
  migrate python /app/v2_resolve_alembic_head.py --config /app/alembic.ini \
  --live-revision "$live_revision"; then
  echo "Refusing to start application roles: the database is not at the repository Alembic head." >&2
  exit 1
fi

"${compose[@]}" up -d web scheduler worker
