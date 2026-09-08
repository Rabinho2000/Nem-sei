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
# The dump is written under a name retention cannot read, and only becomes a
# backup once it has been verified. Writing straight to `$archive` made the
# file eligible the instant the first byte landed: a dump killed halfway --
# the host rebooting, the container going away, the disk filling -- left a
# file with the right name and the right age and the wrong contents, and the
# next retention run counted it among the seven dailies it keeps. The oldest
# good copy was then deleted to make room for it.
partial="$archive.partial"

python3 "$root/scripts/verify_v2_runtime_isolation.py" \
  --v1-data-root "$v1_root" \
  --v2-data-root "$v2_root" \
  --compose-file "$root/docker-compose.v2.yml"
mkdir -p "$backup_dir"

# A dump that dies leaves its partial file behind rather than a half-written
# backup; the trap removes it so the directory does not accumulate them.
cleanup_partial() { rm -f -- "$partial"; }
trap cleanup_partial EXIT

compose=(docker compose --project-name "$project" --env-file "$env_file" -f "$root/docker-compose.v2.yml")
"${compose[@]}" exec -T postgres pg_dump -U nemsei -d nemsei_v2 --format=custom > "$partial"
test -s "$partial"
# Non-zero length is not integrity. `pg_restore --list` reads the custom
# format's table of contents, which a truncated dump does not have, so this is
# the cheapest check that distinguishes a finished archive from a fragment.
pg_restore_out=$("${compose[@]}" exec -T postgres pg_restore --list < "$partial" 2>&1) || {
  echo "Backup archive failed pg_restore --list; not promoting: $partial" >&2
  echo "$pg_restore_out" | tail -5 >&2
  exit 1
}
[[ "$(stat -c '%a' "$partial")" == "600" ]] ||
  { echo "Backup archive must be mode 600: $partial" >&2; exit 1; }
# Verified, so promote it. `mv` within one directory is atomic: retention
# never sees a name it recognises attached to unfinished bytes.
mv -- "$partial" "$archive"
trap - EXIT
test -s "$archive"
[[ "$(stat -c '%a' "$archive")" == "600" ]] ||
  { echo "Backup archive must be mode 600: $archive" >&2; exit 1; }
# Seven daily, four weekly, three monthly -- see scripts/v2_backup_retention.py
# for why the windows count dumps rather than calendar days.
python3 "$root/scripts/v2_backup_retention.py" --directory "$backup_dir" --delete
printf 'Created V2 PostgreSQL backup: %s\n' "$archive"
