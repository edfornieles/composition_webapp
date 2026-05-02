#!/usr/bin/env bash
set -euo pipefail

# Stream service logs from Hetzner.
# Usage:
#   SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 \
#   APP_SERVICE_NAME=composition-webapp SERVICE=all bash scripts/prod/logs_hetzner.sh

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
SERVICE="${SERVICE:-all}"
LINES="${LINES:-200}"

case "$SERVICE" in
  gunicorn) UNITS="${APP_SERVICE_NAME}-gunicorn.service" ;;
  celery) UNITS="${APP_SERVICE_NAME}-celery.service" ;;
  nginx) UNITS="nginx.service" ;;
  redis) UNITS="redis-server.service" ;;
  all) UNITS="${APP_SERVICE_NAME}-gunicorn.service ${APP_SERVICE_NAME}-celery.service nginx.service redis-server.service" ;;
  *)
    echo "Unsupported SERVICE=${SERVICE}. Use one of: gunicorn, celery, nginx, redis, all" >&2
    exit 1
    ;;
esac

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -p "$SSH_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" \
  UNITS="$UNITS" \
  LINES="$LINES" \
  'bash -s' <<'REMOTE'
set -euo pipefail
sudo journalctl --no-pager -n "$LINES" -u $UNITS
echo
echo "---- following logs (Ctrl+C to exit) ----"
sudo journalctl -f -u $UNITS
REMOTE

