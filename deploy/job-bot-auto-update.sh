#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/job-bot/app}"
APP_USER="${APP_USER:-jobbot}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-job-bot.service}"
LOCK_FILE="${LOCK_FILE:-/run/job-bot-auto-update.lock}"

exec 9>"${LOCK_FILE}"
flock -n 9 || exit 0

run_as_app_user() {
  sudo -H -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && $*"
}

if ! run_as_app_user "git rev-parse --is-inside-work-tree >/dev/null 2>&1"; then
  echo "job-bot-auto-update: ${APP_DIR} is not a git checkout" >&2
  exit 1
fi

if ! run_as_app_user "git diff --quiet && git diff --cached --quiet"; then
  echo "job-bot-auto-update: working tree is dirty; refusing to deploy" >&2
  exit 1
fi

current_commit="$(run_as_app_user "git rev-parse HEAD")"
run_as_app_user "git fetch origin '${BRANCH}'"
remote_commit="$(run_as_app_user "git rev-parse origin/'${BRANCH}'")"

if [[ "${current_commit}" == "${remote_commit}" ]]; then
  echo "job-bot-auto-update: already on ${current_commit}"
  exit 0
fi

run_as_app_user "git merge --ff-only origin/'${BRANCH}'"
run_as_app_user "if [[ ! -d .venv ]]; then python3 -m venv .venv; fi"
run_as_app_user ".venv/bin/pip install --upgrade pip"
run_as_app_user ".venv/bin/pip install -r requirements.txt"

sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl is-active --quiet "${SERVICE_NAME}"

echo "job-bot-auto-update: deployed ${remote_commit}"
