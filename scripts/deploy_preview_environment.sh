#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly PREVIEW_ROOT="/opt/server/apps/Nem-sei-preview"
readonly PRODUCTION_ROOT="/opt/server/apps/Nem-sei"
readonly PREVIEW_BRANCH="codex/server-dev-2026-07-29"
readonly COMPOSE_PROJECT="nem-sei-preview"
readonly COMPOSE_FILE="${PREVIEW_ROOT}/docker-compose.preview.yml"
readonly PREVIEW_ENV="${PREVIEW_ROOT}/.env.preview"
readonly PREVIEW_DB="${PREVIEW_ROOT}/runtime/monitoring_board.db"
readonly PRODUCTION_DB="${PRODUCTION_ROOT}/data/monitoring_board.db"
readonly SANITIZE_SQL="${PREVIEW_ROOT}/scripts/sanitize_preview.sql"
readonly PREVIEW_URL="http://127.0.0.1:5002/login"
readonly PRODUCTION_URL="http://127.0.0.1:5000/login"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Executa com sudo."
}

git_preview() {
  local git_user
  git_user="$(stat -c '%U' "${PREVIEW_ROOT}/.git")"
  [[ -n "${git_user}" && "${git_user}" != "UNKNOWN" ]] ||
    die "Não foi possível determinar o proprietário do clone."
  runuser -u "${git_user}" -- git -C "${PREVIEW_ROOT}" "$@"
}

validate_scope() {
  [[ "${PREVIEW_ROOT}" == "/opt/server/apps/Nem-sei-preview" ]] ||
    die "Caminho de preview inesperado."
  [[ -d "${PREVIEW_ROOT}/.git" ]] || die "Clone de preview inexistente."
  [[ -f "${COMPOSE_FILE}" ]] || die "Compose de preview inexistente."
  [[ -f "${PREVIEW_ENV}" ]] || die ".env.preview inexistente."
  [[ -f "${SANITIZE_SQL}" ]] || die "SQL de sanitização inexistente."
  [[ "$(stat -c '%a' "${PREVIEW_ENV}")" == "600" ]] ||
    die ".env.preview deve ter permissões 600."
  [[ "$(git_preview rev-parse --show-toplevel)" == "${PREVIEW_ROOT}" ]] ||
    die "Git root não corresponde ao preview."
  [[ "$(git_preview branch --show-current)" == "${PREVIEW_BRANCH}" ]] ||
    die "Branch de preview inesperada."
  [[ -z "$(git_preview status --porcelain --untracked-files=no)" ]] ||
    die "O preview contém alterações tracked locais."
  grep -qx 'APP_ENV=preview' "${PREVIEW_ENV}" ||
    die "APP_ENV=preview em falta."
  for setting in \
    'PREVIEW_BANNER=true' \
    'SCHEDULER_ENABLED=false' \
    'EXTERNAL_ACTIONS_ENABLED=false' \
    'FUSIONSOLAR_PRODUCTION_SYNC_ENABLED=false' \
    'FUSIONSOLAR_DIAGNOSTICS_SYNC_ENABLED=false' \
    'SIGENERGY_ENABLED=false' \
    'TELEGRAM_ALERTS_ENABLED=false' \
    'TELEGRAM_DAILY_SUMMARY_ENABLED=false'
  do
    grep -qx "${setting}" "${PREVIEW_ENV}" ||
      die "Proteção obrigatória ausente: ${setting}"
  done
}

compose() {
  docker compose \
    --project-name "${COMPOSE_PROJECT}" \
    --env-file "${PREVIEW_ENV}" \
    --file "${COMPOSE_FILE}" \
    "$@"
}

check_url() {
  local url="$1"
  local label="$2"
  curl --fail --silent --show-error --location --max-time 15 \
    --output /dev/null "${url}" ||
    die "${label} não respondeu em ${url}."
}

wait_for_preview() {
  local container_id
  container_id="$(compose ps --quiet monitoring-board)"
  [[ -n "${container_id}" ]] || die "Container de preview não foi criado."
  for _attempt in $(seq 1 30); do
    if [[ "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")" == "healthy" ]]; then
      check_url "${PREVIEW_URL}" "Preview"
      check_url "${PRODUCTION_URL}" "Produção"
      return
    fi
    sleep 2
  done
  compose ps
  compose logs --tail 100 monitoring-board
  die "Preview não ficou healthy."
}

run_schema() {
  compose run --rm --no-deps monitoring-board \
    python -c 'from app import ensure_database; from monitoring_board.runtime import DB_PATH; ensure_database(str(DB_PATH)); print("preview schema ok")'
}

backup_production_database() {
  command -v sqlite3 >/dev/null || die "sqlite3 não está instalado."
  [[ -r "${PRODUCTION_DB}" ]] || die "Base de produção não pode ser lida."
  local temporary_db="${PREVIEW_DB}.new"
  rm -f "${temporary_db}"
  sqlite3 "${PRODUCTION_DB}" ".timeout 30000" ".backup '${temporary_db}'"
  [[ -s "${temporary_db}" ]] || die "Backup SQLite ficou vazio."
  sqlite3 "${temporary_db}" "PRAGMA quick_check;" | grep -qx 'ok' ||
    die "Cópia SQLite falhou quick_check."
  mv -f "${temporary_db}" "${PREVIEW_DB}"
  chmod 600 "${PREVIEW_DB}"
}

sanitize_preview_database() {
  [[ -f "${PREVIEW_DB}" ]] || die "Base de preview inexistente."
  sqlite3 "${PREVIEW_DB}" < "${SANITIZE_SQL}"
  sqlite3 "${PREVIEW_DB}" "PRAGMA quick_check;" | grep -qx 'ok' ||
    die "Cópia SQLite sanitizada falhou quick_check."
}

refresh_data() {
  local copy_uploads="${1:-no}"
  compose stop monitoring-board
  mkdir -p "${PREVIEW_ROOT}/runtime"/{backups,logs,tmp,uploads/generated_reports}
  backup_production_database
  if [[ "${copy_uploads}" == "yes" ]]; then
    command -v rsync >/dev/null || die "rsync não está instalado."
    rsync --archive --delete \
      "${PRODUCTION_ROOT}/data/uploads/" \
      "${PREVIEW_ROOT}/runtime/uploads/"
  fi
  run_schema
  sanitize_preview_database
  compose up --detach monitoring-board
  wait_for_preview
}

update_preview() {
  local update_completed=false

  recover_preview_update() {
    local exit_code="$1"
    trap - ERR
    printf 'ERROR: atualização do preview falhou (código %s).\n' "${exit_code}" >&2
    compose ps >&2 || true
    compose logs --tail 100 monitoring-board >&2 || true
    printf 'A tentar recuperar apenas o serviço de preview...\n' >&2
    compose up --detach monitoring-board >&2 || true
    compose ps >&2 || true
    curl --fail --silent --show-error --location --max-time 15 \
      --output /dev/null "${PRODUCTION_URL}" ||
      printf 'AVISO: não foi possível reconfirmar a produção após a falha.\n' >&2
    return "${exit_code}"
  }

  trap 'exit_code=$?; if [[ "${update_completed}" != true ]]; then recover_preview_update "${exit_code}"; fi' ERR
  check_url "${PRODUCTION_URL}" "Produção"
  git_preview fetch origin "${PREVIEW_BRANCH}"
  git_preview merge --ff-only "origin/${PREVIEW_BRANCH}"
  validate_scope
  compose build monitoring-board
  compose stop monitoring-board
  run_schema
  compose up --detach --remove-orphans monitoring-board
  wait_for_preview
  check_url "${PRODUCTION_URL}" "Produção"
  update_completed=true
  trap - ERR
}

show_status() {
  compose ps
  check_url "${PRODUCTION_URL}" "Produção"
  check_url "${PREVIEW_URL}" "Preview"
  printf 'Produção: http://media:5000\nPreview:  http://media:5002\n'
}

main() {
  require_root
  validate_scope
  case "${1:-}" in
    update)
      update_preview
      ;;
    refresh-data)
      refresh_data "${2:-no}"
      ;;
    start)
      check_url "${PRODUCTION_URL}" "Produção"
      compose up --detach monitoring-board
      wait_for_preview
      ;;
    stop)
      compose stop monitoring-board
      check_url "${PRODUCTION_URL}" "Produção"
      ;;
    status)
      show_status
      ;;
    *)
      die "Uso: deploy-nem-sei-preview {update|refresh-data [yes]|start|stop|status}"
      ;;
  esac
}

main "$@"
