#!/usr/bin/env sh
set -eu

DATA_DIR="${DATA_DIR:-/data}"
BACKUP_DIR="${BACKUP_DIR:-$DATA_DIR/backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-14}"
DELETE_OLDER_THAN_DAYS="${DELETE_OLDER_THAN_DAYS:-30}"
INCLUDE_UPLOADS="${INCLUDE_UPLOADS:-1}"

DB_PATH="$DATA_DIR/monitoring_board.db"
UPLOADS_DIR="$DATA_DIR/uploads"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

DB_BACKUP="$BACKUP_DIR/monitoring_board_${TIMESTAMP}.db"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required to create a consistent SQLite backup." >&2
  exit 1
fi

sqlite3 "$DB_PATH" ".backup '$DB_BACKUP'"
sqlite3 "$DB_BACKUP" "PRAGMA integrity_check;" | grep -qx "ok"

if [ "$INCLUDE_UPLOADS" = "1" ] && [ -d "$UPLOADS_DIR" ]; then
  tar -C "$DATA_DIR" -czf "$BACKUP_DIR/uploads_${TIMESTAMP}.tar.gz" uploads
fi

find "$BACKUP_DIR" -type f \( -name 'monitoring_board_*.db' -o -name 'uploads_*.tar.gz' \) -mtime +"$DELETE_OLDER_THAN_DAYS" -delete

if [ "$KEEP_BACKUPS" -gt 0 ] 2>/dev/null; then
  ls -1t "$BACKUP_DIR"/monitoring_board_*.db 2>/dev/null | awk "NR>$KEEP_BACKUPS" | while IFS= read -r old_db; do
    rm -f "$old_db"
  done

  ls -1t "$BACKUP_DIR"/uploads_*.tar.gz 2>/dev/null | awk "NR>$KEEP_BACKUPS" | while IFS= read -r old_uploads; do
    rm -f "$old_uploads"
  done
fi

echo "Database backup: $DB_BACKUP"
if [ "$INCLUDE_UPLOADS" = "1" ] && [ -d "$UPLOADS_DIR" ]; then
  echo "Uploads backup: $BACKUP_DIR/uploads_${TIMESTAMP}.tar.gz"
fi
