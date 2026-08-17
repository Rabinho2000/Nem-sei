#!/usr/bin/env bash
set -euo pipefail

root=$(git rev-parse --show-toplevel)
temp_root=$(mktemp -d "${TMPDIR:-/tmp}/nemsei-v2-docker-acceptance.XXXXXX")
project="nemsei-v2-acceptance-$RANDOM"
env_file="$temp_root/.env"

cleanup() {
  docker compose --project-name "$project" --profile acceptance --env-file "$env_file" -f "$root/docker-compose.v2.yml" -f "$root/docker-compose.v2.acceptance.yml" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$temp_root"
}
trap cleanup EXIT

mkdir -p "$temp_root/v1-data" "$temp_root/v2-data" "$temp_root/secrets"
printf '%s' 'nemsei-acceptance' > "$temp_root/secrets/postgres_password"
printf '%s' 'postgresql+psycopg://nemsei:nemsei-acceptance@postgres:5432/nemsei_v2' > "$temp_root/secrets/database_url"
printf '%s\n' \
  NEMSEI_V2_ENV=test \
  NEMSEI_V2_ENV_FILE=$env_file \
  NEMSEI_V2_HOST_DATA_ROOT=$temp_root/v2-data \
  NEMSEI_V2_DATABASE_URL_SECRET_FILE=$temp_root/secrets/database_url \
  NEMSEI_V2_POSTGRES_PASSWORD_SECRET_FILE=$temp_root/secrets/postgres_password \
  NEMSEI_V2_SECRET_KEY=acceptance-secret \
  NEMSEI_V2_ADMIN_USERNAME=admin \
  NEMSEI_V2_ADMIN_PASSWORD_HASH= \
  NEMSEI_V2_PROVIDER_READS=false \
  NEMSEI_V2_PROVIDER_MUTATIONS=false \
  NEMSEI_V2_NOTIFICATIONS=false \
  NEMSEI_V2_REPORT_DISTRIBUTION=false \
  NEMSEI_V2_TESTING=true \
NEMSEI_V2_WORKER_LEASE_SECONDS=3 > "$env_file"

PYTHONPATH="$root/src" python3 "$root/scripts/verify_v2_runtime_isolation.py" \
  --v1-data-root "$temp_root/v1-data" \
  --v2-data-root "$temp_root/v2-data" \
  --compose-file "$root/docker-compose.v2.yml"

compose=(docker compose --project-name "$project" --profile acceptance --env-file "$env_file" -f "$root/docker-compose.v2.yml" -f "$root/docker-compose.v2.acceptance.yml")
"${compose[@]}" build worker migrate
"${compose[@]}" run --rm migrate
"${compose[@]}" up -d worker
"${compose[@]}" exec -T worker python -c '
from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.repository import JobRepository
s=Settings.from_environment(); e=build_engine(s); r=JobRepository(e, build_session_factory(e)); j,_=r.enqueue(job_type="system.noop", payload={"test_hold_seconds": 8}, actor_source="system", dedupe_key="docker-recovery"); print(j.id)
'

for _ in $(seq 1 30); do
  running=$("${compose[@]}" exec -T worker python -c '
from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job
from sqlalchemy import select
s=Settings.from_environment(); e=build_engine(s)
with build_session_factory(e)() as session: print(session.scalar(select(Job.status).where(Job.dedupe_key == "docker-recovery")) or "")
')
  [ "$running" = "running" ] && break
  sleep 1
done
[ "${running:-}" = "running" ]
"${compose[@]}" kill -s SIGKILL worker
sleep 4
"${compose[@]}" up -d worker

for _ in $(seq 1 30); do
  outcome=$("${compose[@]}" exec -T worker python -c '
from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job, JobEvent
from sqlalchemy import select
s=Settings.from_environment(); e=build_engine(s)
with build_session_factory(e)() as session:
 j=session.scalar(select(Job).where(Job.dedupe_key == "docker-recovery")); ev=list(session.scalars(select(JobEvent).where(JobEvent.job_id == j.id).order_by(JobEvent.id))); print(j.status, j.attempt_count, [(x.actor_source,x.from_status,x.to_status) for x in ev])
')
  case "$outcome" in *"success 2"*"('recovery', 'running', 'waiting')"*) break;; esac
  sleep 1
done
case "${outcome:-}" in *"success 2"*"('recovery', 'running', 'waiting')"*) echo "Docker stale-worker recovery acceptance passed";; *) echo "Docker stale-worker recovery acceptance failed: ${outcome:-no outcome}" >&2; exit 1;; esac
