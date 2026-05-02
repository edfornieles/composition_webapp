#!/usr/bin/env bash
set -euo pipefail

# Restart production services on Hetzner.
# Usage:
#   SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 \
#   APP_SERVICE_NAME=composition-webapp bash scripts/prod/restart_hetzner.sh

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required env var: ${name}" >&2
    exit 1
  fi
}

require_var SSH_KEY_PATH
require_var SSH_USER
require_var SSH_HOST

SSH_PORT="${SSH_PORT:-22}"
APP_SERVICE_NAME="${APP_SERVICE_NAME:-composition-webapp}"

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -p "$SSH_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" \
  APP_SERVICE_NAME="$APP_SERVICE_NAME" \
  'bash -s' <<'REMOTE'
set -euo pipefail
sudo systemctl restart "${APP_SERVICE_NAME}-gunicorn.service"
sudo systemctl restart "${APP_SERVICE_NAME}-celery.service"
sudo systemctl restart nginx
sudo systemctl restart redis-server
sudo systemctl --no-pager --full status "${APP_SERVICE_NAME}-gunicorn.service" "${APP_SERVICE_NAME}-celery.service" nginx redis-server | sed -n '1,120p'
REMOTE

