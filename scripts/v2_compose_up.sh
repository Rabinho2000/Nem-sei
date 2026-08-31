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
v2_real=$(realpath -m "$v2_root")

# Which compose files a canonical deploy carries is not this script's opinion
# and not the operator's memory -- it is deploy/v2_deployment_components.json,
# read here. Before this, the wrapper hardcoded docker-compose.v2.yml alone,
# so a canonical deploy recreated scheduler and worker *without*
# docker-compose.v2.diagnostic-incidents.yml and silently turned the incident
# evaluator off. Nothing failed; the interface went on reporting the automation
# as running. It happened on 2026-08-21, and it had happened again by
# 2026-08-31.
components=(python3 "$root/scripts/v2_deployment_components.py" --root "$root")
mapfile -t compose_files < <("${components[@]}" compose-files)
if (( ${#compose_files[@]} == 0 )); then
  echo "The deployment manifest declared no compose files." >&2
  exit 1
fi

compose=(docker compose --project-name nemsei-v2 --env-file "$env_file")
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$root/$compose_file")
done
# Isolation is checked against every file the deployment carries, now that
# there is more than one, rather than against the base file alone.
isolation_files=()
for compose_file in "${compose_files[@]}"; do
  isolation_files+=("$root/$compose_file")
done
python3 "$root/scripts/verify_v2_runtime_isolation.py" --v1-data-root "$v1_root" \
  --v2-data-root "$v2_root" --compose-file "${isolation_files[@]}"

rendered=$("${compose[@]}" config --format json)
RENDERED_COMPOSE="$rendered" "${components[@]}" check-rendered
RENDERED_COMPOSE="$rendered" python3 - "$v2_real" <<'PY'
import json
import os
import sys

expected = os.path.realpath(sys.argv[1])
config = json.loads(os.environ["RENDERED_COMPOSE"])
if config["services"]["postgres"].get("ports"):
    raise SystemExit("PostgreSQL must not publish a host port")
PY

# Does this deployment declare the Huawei SCADA listener?
#
# The listener used to be invisible to this wrapper: it sits behind a profile,
# so every deploy rebuilt and recreated web, scheduler and worker while the
# listener kept serving the previous build, indefinitely and silently. Starting
# it unconditionally is not the fix -- it is the only inbound port this system
# opens, and opening it has to stay a deliberate act. So the wrapper asks
# whether this deployment declares SCADA, and then does the whole job either
# way. `NEMSEI_V2_HUAWEI_SCADA_CONNECTION_ID` is that declaration: the listener
# already refuses to run without it, so there is no second switch to keep in
# agreement.
#
# A deployment that does not declare SCADA is left exactly alone here -- if an
# operator started a listener by hand, tearing it down is not this script's
# call to make.
scada_profile=huawei-scada
scada_rendered=$("${compose[@]}" --profile "$scada_profile" config --format json)
scada_declared=$(SCADA_RENDERED_COMPOSE="$scada_rendered" \
  python3 "$root/scripts/v2_scada_deployment_intent.py")
scada_declared=${scada_declared##*=}
unset scada_rendered

# Build every image this deployment is about to run, including the `migrate`
# service that sits behind the `manual` profile and that a plain
# `docker compose build` silently skips. A stale migrate image runs
# `alembic upgrade head` against its own older migration graph, exits 0, and
# leaves the database behind the checked-out code; a stale web image carries an
# older graph than the migrated database and fails its readiness check.
"${compose[@]}" --profile manual build
if [[ $scada_declared == true ]]; then
  "${compose[@]}" --profile "$scada_profile" build scada-listener
fi
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

# `check-rendered` above proved compose was *told* about every required
# component. This proves each one reached the process that is now running --
# the case that matters when a service was already up and nothing in the new
# configuration forced it to be recreated.
#
# The read is retried, because `up -d` returns as soon as the container is
# started and `exec` into one that has not finished starting fails. Found on
# the first real run of this gate: the scheduler answered and the worker, a
# second behind it, did not, and the deploy failed over a variable that was in
# fact correctly set. A container that never answers still fails the check --
# it just has to actually never answer.
read_component_value() {
  local service=$1 variable=$2 deadline value
  deadline=$((SECONDS + ${NEMSEI_V2_COMPONENT_READY_TIMEOUT_SECONDS:-60}))
  while :; do
    # `< /dev/null` is load-bearing: `docker compose exec` reads standard
    # input, and this loop's standard input is the list of assertions still to
    # be checked. Without it the first `exec` swallowed the rest of the list,
    # the loop ended after one service, and the second was reported as running
    # without a variable it had -- which is exactly how this gate failed the
    # first two deploys it ever ran.
    if value=$("${compose[@]}" exec -T "$service" printenv "$variable" 2>/dev/null < /dev/null); then
      printf '%s' "$value" | tr -d '\r' | head -n 1
      return 0
    fi
    (( SECONDS >= deadline )) && return 1
    sleep 2
  done
}

observations=$(while IFS=$'\t' read -r service variable _expected; do
  [[ -n $service ]] || continue
  # `printenv` exits non-zero for a variable the container does not define, and
  # so does `exec` into a container that is not there at all; both end up as a
  # line with no value, which check-live reports as "running without".
  if value=$(read_component_value "$service" "$variable") && [[ -n $value ]]; then
    printf '%s\t%s\t%s\n' "$service" "$variable" "$value"
  else
    printf '%s\t%s\n' "$service" "$variable"
  fi
done < <("${components[@]}" assertions))
printf '%s\n' "$observations" | "${components[@]}" check-live

# The listener joins the same deployment, and its health is a deploy gate: a
# deployment that declares SCADA and ends without a listener has lost readings
# that no later run can collect again, and nothing else in the stack reports
# the gap. `restart: unless-stopped` means a crash loop still shows `running`
# between attempts, so the state is confirmed twice with a settle in between.
if [[ $scada_declared == true ]]; then
  "${compose[@]}" --profile "$scada_profile" up -d scada-listener
  deadline=$((SECONDS + ${NEMSEI_V2_SCADA_READY_TIMEOUT_SECONDS:-60}))
  while :; do
    scada_state=$("${compose[@]}" --profile "$scada_profile" ps \
      --format '{{.State}}' scada-listener | tr -d '\r' | tail -n 1)
    if [[ $scada_state == running ]]; then
      sleep 5
      scada_state=$("${compose[@]}" --profile "$scada_profile" ps \
        --format '{{.State}}' scada-listener | tr -d '\r' | tail -n 1)
      [[ $scada_state == running ]] && break
    fi
    if (( SECONDS >= deadline )); then
      echo "This deployment declares Huawei SCADA but scada-listener is '${scada_state:-absent}'." >&2
      "${compose[@]}" --profile "$scada_profile" logs --tail 20 scada-listener >&2 || true
      exit 1
    fi
    sleep 2
  done
fi
