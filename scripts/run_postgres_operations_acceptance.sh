#!/usr/bin/env bash
# Exercise PostgreSQL restart persistence and V2 logical backup/restore.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
temp_root=$(mktemp -d "${TMPDIR:-/tmp}/nemsei-v2-postgres-acceptance.XXXXXX")
project="nemsei-v2-postgres-acceptance-$RANDOM"
env_file="$temp_root/.env"

cleanup() {
  docker compose --project-name "$project" --env-file "$env_file" -f "$root/docker-compose.v2.yml" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$temp_root"
}
trap cleanup EXIT

mkdir -p "$temp_root/v1-data" "$temp_root/v2-data" "$temp_root/secrets"
printf '%s' 'nemsei-operations' > "$temp_root/secrets/postgres_password"
printf '%s' 'postgresql+psycopg://nemsei:nemsei-operations@postgres:5432/nemsei_v2' > "$temp_root/secrets/database_url"
# Empty stand-ins for the provider credentials. Compose bind-mounts every
# secret a service declares whether or not the run will read one, so without
# these files nothing starts on a clean checkout. Empty is the point: an
# acceptance run that could authenticate against a real provider would be
# testing the provider.
for placeholder in v1_broker_token fusionsolar_username fusionsolar_password \
    telegram_bot_token sigenergy_app_key sigenergy_app_secret; do
  : > "$temp_root/secrets/$placeholder"
done
printf '%s\n' \
  NEMSEI_V2_ENV=test \
  NEMSEI_V2_ENV_FILE=$env_file \
  NEMSEI_V2_COMPOSE_PROJECT=$project \
  NEMSEI_V1_DATA_ROOT=$temp_root/v1-data \
  NEMSEI_V2_HOST_DATA_ROOT=$temp_root/v2-data \
  NEMSEI_V2_WEB_PORT=15002 \
  NEMSEI_V2_DATABASE_URL_SECRET_FILE=$temp_root/secrets/database_url \
  NEMSEI_V2_POSTGRES_PASSWORD_SECRET_FILE=$temp_root/secrets/postgres_password \
  NEMSEI_V2_V1_BROKER_TOKEN_SECRET_FILE=$temp_root/secrets/v1_broker_token \
  NEMSEI_V2_FUSIONSOLAR_USERNAME_SECRET_FILE=$temp_root/secrets/fusionsolar_username \
  NEMSEI_V2_FUSIONSOLAR_PASSWORD_SECRET_FILE=$temp_root/secrets/fusionsolar_password \
  NEMSEI_V2_TELEGRAM_BOT_TOKEN_SECRET_FILE=$temp_root/secrets/telegram_bot_token \
  NEMSEI_V2_SIGENERGY_APP_KEY_SECRET_FILE=$temp_root/secrets/sigenergy_app_key \
  NEMSEI_V2_SIGENERGY_APP_SECRET_SECRET_FILE=$temp_root/secrets/sigenergy_app_secret \
  NEMSEI_V2_SECRET_KEY=acceptance-secret \
  NEMSEI_V2_ADMIN_USERNAME=admin \
  NEMSEI_V2_ADMIN_PASSWORD_HASH=scrypt:32768:8:1\$\$J2DXwj5xCWb4a6C2\$\$95e8a0680b79067c448f042dae3efdffc72a85da3018a478d07a46007511d4ff61f5ea7d59b5097142f5dc130beb697e3aecbb08c89ab2409db7b353063e8abc \
  NEMSEI_V2_TESTING=true > "$env_file"

compose=(docker compose --project-name "$project" --env-file "$env_file" -f "$root/docker-compose.v2.yml")
"${compose[@]}" build migrate
"${compose[@]}" run --rm migrate
"${compose[@]}" up -d web
for _ in $(seq 1 30); do
  curl --fail --silent http://127.0.0.1:15002/healthz >/dev/null && \
    curl --fail --silent http://127.0.0.1:15002/readyz >/dev/null && break
  sleep 1
done
curl --fail --silent http://127.0.0.1:15002/healthz >/dev/null
curl --fail --silent http://127.0.0.1:15002/readyz >/dev/null
"${compose[@]}" run --rm migrate python -c '
from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.repository import JobRepository
s = Settings.from_environment(); e = build_engine(s)
r = JobRepository(e, build_session_factory(e))
j, created = r.enqueue(job_type="system.noop", payload={}, actor_source="system", dedupe_key="postgres-operations")
assert created and j.id
'
"${compose[@]}" restart postgres
for _ in $(seq 1 30); do
  status=$("${compose[@]}" ps --format json postgres | python3 -c 'import json,sys; rows=json.load(sys.stdin); rows = rows if isinstance(rows, list) else [rows]; print(rows[0].get("Health", "") if rows else "")')
  [ "$status" = healthy ] && break
  sleep 1
done
[ "${status:-}" = healthy ]
persisted=$("${compose[@]}" exec -T postgres psql -U nemsei -d nemsei_v2 -At -c "SELECT COUNT(*) FROM jobs WHERE dedupe_key = 'postgres-operations'")
[ "$persisted" = 1 ]

NEMSEI_V2_ENV_FILE="$env_file" \
NEMSEI_V2_COMPOSE_PROJECT="$project" \
NEMSEI_V1_DATA_ROOT="$temp_root/v1-data" \
NEMSEI_V2_HOST_DATA_ROOT="$temp_root/v2-data" \
  "$root/scripts/v2_postgres_backup.sh"
archive=$(find "$temp_root/v2-data/backups" -maxdepth 1 -type f -name 'nemsei-v2-*.dump' -print -quit)
test -n "$archive"
NEMSEI_V2_ENV_FILE="$env_file" \
NEMSEI_V2_COMPOSE_PROJECT="$project" \
  "$root/scripts/v2_postgres_restore_smoke.sh" "$archive"
printf 'PostgreSQL restart-persistence and backup/restore acceptance passed\n'
