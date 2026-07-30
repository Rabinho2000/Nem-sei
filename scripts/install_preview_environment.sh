#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly PREVIEW_ROOT="/opt/server/apps/Nem-sei-preview"
readonly PRODUCTION_ROOT="/opt/server/apps/Nem-sei"
readonly PREVIEW_BRANCH="codex/server-dev-2026-07-29"
readonly DEPLOY_TARGET="/usr/local/bin/deploy-nem-sei-preview"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "Executa este instalador com sudo."
[[ -d "${PRODUCTION_ROOT}/.git" ]] ||
  die "Repositório de produção não encontrado; nada foi alterado."
[[ -f "${PRODUCTION_ROOT}/data/monitoring_board.db" ]] ||
  die "Base SQLite de produção não encontrada; nada foi alterado."
command -v docker >/dev/null || die "Docker não está instalado."
docker compose version >/dev/null || die "Docker Compose v2 não está disponível."
command -v sqlite3 >/dev/null || die "sqlite3 não está instalado."
command -v curl >/dev/null || die "curl não está instalado."

operator="${SUDO_USER:-}"
[[ -n "${operator}" && "${operator}" != "root" ]] ||
  die "Executa com sudo a partir do utilizador administrativo."
operator_group="$(id -gn "${operator}")"
repository_url="$(
  sudo -u "${operator}" git -C "${PRODUCTION_ROOT}" remote get-url origin
)"
[[ -n "${repository_url}" ]] || die "Remote origin de produção não encontrado."

if [[ -e "${PREVIEW_ROOT}" ]]; then
  [[ -d "${PREVIEW_ROOT}/.git" ]] ||
    die "${PREVIEW_ROOT} existe mas não é um clone Git."
else
  install -d -m 0750 -o "${operator}" -g "${operator_group}" "${PREVIEW_ROOT}"
  sudo -u "${operator}" git clone \
    --branch "${PREVIEW_BRANCH}" \
    --single-branch \
    "${repository_url}" \
    "${PREVIEW_ROOT}"
fi

[[ "$(
  sudo -u "${operator}" git -C "${PREVIEW_ROOT}" rev-parse --show-toplevel
)" == "${PREVIEW_ROOT}" ]] ||
  die "Clone de preview inválido."
[[ "$(
  sudo -u "${operator}" git -C "${PREVIEW_ROOT}" branch --show-current
)" == "${PREVIEW_BRANCH}" ]] ||
  die "O clone existente não está na branch de preview."
[[ -z "$(
  sudo -u "${operator}" git -C "${PREVIEW_ROOT}" status --porcelain --untracked-files=no
)" ]] ||
  die "O clone de preview contém alterações tracked."

read -r -p "APP_USERNAME do preview: " app_username
[[ "${app_username}" =~ ^[A-Za-z0-9._@+-]{1,80}$ ]] ||
  die "APP_USERNAME contém caracteres inválidos."
read -r -s -p "APP_PASSWORD do preview: " app_password
printf '\n'
[[ ${#app_password} -ge 12 ]] ||
  die "APP_PASSWORD deve ter pelo menos 12 caracteres."
[[ "${app_password}" != *$'\r'* && "${app_password}" != *$'\n'* ]] ||
  die "APP_PASSWORD contém newline inválido."
[[ ! "${app_password}" =~ [[:space:]#] ]] ||
  die "APP_PASSWORD não pode conter espaços nem # neste instalador."
flask_secret="$(openssl rand -hex 32)"

cat > "${PREVIEW_ROOT}/.env.preview" <<EOF
FLASK_SECRET_KEY=${flask_secret}
APP_USERNAME=${app_username}
APP_PASSWORD=${app_password}
APP_ENV=preview
PREVIEW_BANNER=true
SCHEDULER_ENABLED=false
EXTERNAL_ACTIONS_ENABLED=false
DATA_DIR=/data
SESSION_COOKIE_SECURE=false
FUSIONSOLAR_USERNAME=
FUSIONSOLAR_PASSWORD=
FUSIONSOLAR_BASE_URL=
FUSIONSOLAR_PRODUCTION_SYNC_ENABLED=false
FUSIONSOLAR_DIAGNOSTICS_SYNC_ENABLED=false
SIGENERGY_ENABLED=false
SIGENERGY_APP_KEY=
SIGENERGY_APP_SECRET=
SIGENERGY_HISTORY_ENERGY_UNIT=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ALERTS_ENABLED=false
TELEGRAM_DAILY_SUMMARY_ENABLED=false
OPENROUTESERVICE_API_KEY=
EOF
chmod 600 "${PREVIEW_ROOT}/.env.preview"
chown root:root "${PREVIEW_ROOT}/.env.preview"

install -d -m 0750 -o root -g root \
  "${PREVIEW_ROOT}/runtime" \
  "${PREVIEW_ROOT}/runtime/backups" \
  "${PREVIEW_ROOT}/runtime/logs" \
  "${PREVIEW_ROOT}/runtime/tmp" \
  "${PREVIEW_ROOT}/runtime/uploads" \
  "${PREVIEW_ROOT}/runtime/uploads/generated_reports"

install -m 0755 -o root -g root \
  "${PREVIEW_ROOT}/scripts/deploy_preview_environment.sh" \
  "${DEPLOY_TARGET}"

read -r -p "Copiar uploads de produção para o preview? [y/N] " copy_uploads
copy_uploads_arg="no"
if [[ "${copy_uploads,,}" == "y" || "${copy_uploads,,}" == "yes" ]]; then
  command -v rsync >/dev/null || die "rsync é necessário para copiar uploads."
  copy_uploads_arg="yes"
fi

"${DEPLOY_TARGET}" refresh-data "${copy_uploads_arg}"
"${DEPLOY_TARGET}" update
"${DEPLOY_TARGET}" status

printf '\nInstalação concluída.\n'
printf 'Produção: http://media:5000\n'
printf 'Preview:  http://media:5002\n'
printf 'Limita a porta 5002 à LAN/Tailscale na firewall do servidor.\n'
