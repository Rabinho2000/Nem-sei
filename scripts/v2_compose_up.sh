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
  echo "SQLite V2 supports exactly one worker; scaling/concurrency must remain 1." >&2
  exit 2
fi

mkdir -p "$v2_root"
v1_real=$(realpath -m "$v1_root")
v2_real=$(realpath -m "$v2_root")
database="$v2_real/nemsei_v2.db"

PYTHONPATH="$root/src" python3 "$root/scripts/verify_v2_runtime_isolation.py" \
  --v1-data-root "$v1_real" \
  --v2-data-root "$v2_real" \
  --database "$database" \
  --compose-file "$root/docker-compose.v2.yml"

compose=(docker compose --project-name nemsei-v2 --env-file "$env_file" -f "$root/docker-compose.v2.yml")
rendered=$("${compose[@]}" config --format json)
RENDERED_COMPOSE="$rendered" python3 - "$v2_real" <<'PY'
import json
import os
import sys

expected = os.path.realpath(sys.argv[1])
config = json.loads(os.environ["RENDERED_COMPOSE"])
for role in ("web", "scheduler", "worker", "migrate"):
    mounts = config["services"][role].get("volumes", [])
    matches = [mount for mount in mounts if mount.get("target") == "/data" and mount.get("type") == "bind"]
    if len(matches) != 1 or os.path.realpath(matches[0]["source"]) != expected:
        raise SystemExit(f"{role} does not bind the validated V2 root at /data")
PY

"${compose[@]}" run --rm migrate
"${compose[@]}" up -d web scheduler worker
