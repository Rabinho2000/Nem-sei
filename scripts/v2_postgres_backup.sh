#!/usr/bin/env bash
# Create a V2-only logical PostgreSQL backup and retain the seven newest days.
set -euo pipefail
# A backup holds the whole operational database, so it must never be created
# world-readable and then tightened afterwards.
umask 077

# A systemd timer runs this as root, where `git rev-parse` refuses a repository
# owned by somebody else. The override keeps git out of the backup path entirely.
root=${NEMSEI_V2_REPO_ROOT:-$(git rev-parse --show-toplevel)}
v1_root=${NEMSEI_V1_DATA_ROOT:-/opt/server/apps/Nem-sei/data}
v2_root=${NEMSEI_V2_HOST_DATA_ROOT:-/opt/server/apps/Nem-sei-v2-data}
env_file=${NEMSEI_V2_ENV_FILE:-$root/.env.v2}
project=${NEMSEI_V2_COMPOSE_PROJECT:-nemsei-v2}
backup_dir="$v2_root/backups"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$backup_dir/nemsei-v2-$timestamp.dump"

python3 "$root/scripts/verify_v2_runtime_isolation.py" \
  --v1-data-root "$v1_root" \
  --v2-data-root "$v2_root" \
  --compose-file "$root/docker-compose.v2.yml"
mkdir -p "$backup_dir"

compose=(docker compose --project-name "$project" --env-file "$env_file" -f "$root/docker-compose.v2.yml")
"${compose[@]}" exec -T postgres pg_dump -U nemsei -d nemsei_v2 --format=custom > "$archive"
test -s "$archive"
[[ "$(stat -c '%a' "$archive")" == "600" ]] ||
  { echo "Backup archive must be mode 600: $archive" >&2; exit 1; }
# Seven daily, four weekly, three monthly -- see scripts/v2_backup_retention.py
# for why the windows count dumps rather than calendar days.
python3 "$root/scripts/v2_backup_retention.py" --directory "$backup_dir" --delete
printf 'Created V2 PostgreSQL backup: %s\n' "$archive"
