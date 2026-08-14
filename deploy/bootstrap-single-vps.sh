#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this script as root (for example with sudo)" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

APP_NAME="${APP_NAME:-job-bot}"
APP_USER="${APP_USER:-jobbot}"
DEPLOY_USER="${DEPLOY_USER:-${APP_USER}}"
APP_DIR="${APP_DIR:-/opt/job-bot/app}"
APP_PARENT="$(dirname "${APP_DIR}")"
ENV_DIR="${ENV_DIR:-/etc/job-bot}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/job-bot.env}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${APP_NAME}.service"
AUTO_UPDATE_SERVICE_NAME="${APP_NAME}-auto-update.service"
AUTO_UPDATE_TIMER_NAME="${APP_NAME}-auto-update.timer"
LOCK_FILE="/run/${APP_NAME}-auto-update.lock"

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  git \
  python3 \
  python3-pip \
  python3-venv \
  rsync \
  sudo \
  ufw

python3 - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit("python3.11+ is required")
PY

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

if ! getent group docker >/dev/null 2>&1; then
  groupadd docker
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${APP_USER}"
fi

usermod -aG docker "${APP_USER}"

mkdir -p "${APP_PARENT}" "${APP_DIR}" "${ENV_DIR}"

if [[ "${ROOT_DIR}" != "${APP_DIR}" ]]; then
  rsync -az \
    --exclude ".venv" \
    --exclude ".pytest_cache" \
    --exclude "__pycache__" \
    --exclude "job_bot.db" \
    "${ROOT_DIR}/" "${APP_DIR}/"
fi

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${APP_DIR}/deploy/prod.env.example" "${ENV_FILE}"
fi

chown root:"${APP_USER}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

ln -sfn "${ENV_FILE}" "${APP_DIR}/.env"

render_template() {
  local source_path="$1"
  local target_path="$2"

  sed \
    -e "s#__APP_DIR__#${APP_DIR//\//\\/}#g" \
    -e "s#__APP_USER__#${APP_USER//\//\\/}#g" \
    -e "s#__ENV_FILE__#${ENV_FILE//\//\\/}#g" \
    -e "s#__BRANCH__#${BRANCH//\//\\/}#g" \
    -e "s#__SERVICE_NAME__#${SERVICE_NAME//\//\\/}#g" \
    -e "s#__AUTO_UPDATE_SERVICE_NAME__#${AUTO_UPDATE_SERVICE_NAME//\//\\/}#g" \
    -e "s#__LOCK_FILE__#${LOCK_FILE//\//\\/}#g" \
    "${source_path}" >"${target_path}"
}

render_template "${APP_DIR}/deploy/job-bot.service" "/etc/systemd/system/${SERVICE_NAME}"
render_template "${APP_DIR}/deploy/job-bot-auto-update.service" "/etc/systemd/system/${AUTO_UPDATE_SERVICE_NAME}"
render_template "${APP_DIR}/deploy/job-bot-auto-update.timer" "/etc/systemd/system/${AUTO_UPDATE_TIMER_NAME}"

chmod +x "${APP_DIR}/deploy/job-bot-auto-update.sh" "${APP_DIR}/deploy/bootstrap-single-vps.sh"

runuser -u "${APP_USER}" -- bash -lc "cd '${APP_DIR}' && python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt"

cat >/etc/sudoers.d/${DEPLOY_USER}-${APP_NAME} <<EOF
${DEPLOY_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload, /usr/bin/systemctl enable ${SERVICE_NAME} ${AUTO_UPDATE_TIMER_NAME}, /usr/bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl start ${AUTO_UPDATE_SERVICE_NAME}, /usr/bin/systemctl restart ${AUTO_UPDATE_TIMER_NAME}, /usr/bin/systemctl start ${AUTO_UPDATE_TIMER_NAME}, /usr/bin/journalctl -u ${SERVICE_NAME}, /usr/bin/journalctl -u ${AUTO_UPDATE_SERVICE_NAME}, /usr/bin/journalctl -u ${AUTO_UPDATE_TIMER_NAME}
EOF
chmod 440 /etc/sudoers.d/${DEPLOY_USER}-${APP_NAME}

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" "${AUTO_UPDATE_TIMER_NAME}"

cat <<EOF

single-vps bootstrap complete

- app user: ${APP_USER}
- deploy user: ${DEPLOY_USER}
- app directory: ${APP_DIR}
- env file: ${ENV_FILE}
- service: ${SERVICE_NAME}
- auto-update timer: ${AUTO_UPDATE_TIMER_NAME}

next steps:
1. edit the production env file at ${ENV_FILE}
2. if the repo is private, add a read-only deploy key or clone with a credential the server can reuse for git fetch
3. start the bot and confirm it is healthy with:
   sudo systemctl restart ${SERVICE_NAME}
   sudo systemctl status ${SERVICE_NAME}
   sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager
4. start the auto-update timer when you are ready:
   sudo systemctl start ${AUTO_UPDATE_TIMER_NAME}
5. if you want GitHub Actions deploys, add the secrets documented in deploy/single-vps.md
EOF
