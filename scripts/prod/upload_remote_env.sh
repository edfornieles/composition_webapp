#!/usr/bin/env bash
set -euo pipefail

# Securely upload a completed production env file to the VPS.
#
# Usage:
#   SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 \
#   LOCAL_ENV_FILE=/path/to/production.env \
#   bash scripts/prod/upload_remote_env.sh

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
require_var LOCAL_ENV_FILE

SSH_PORT="${SSH_PORT:-22}"
APP_DIR="${APP_DIR:-/srv/composition_webapp-main}"
APP_USER="${APP_USER:-$SSH_USER}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-$APP_DIR/djangoscrap/.env}"

if [[ ! -f "$SSH_KEY_PATH" ]]; then
  echo "SSH key not found at SSH_KEY_PATH: $SSH_KEY_PATH" >&2
  exit 1
fi
if [[ ! -f "$LOCAL_ENV_FILE" ]]; then
  echo "Local env file not found at LOCAL_ENV_FILE: $LOCAL_ENV_FILE" >&2
  exit 1
fi

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -p "$SSH_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

echo "Uploading production env to ${SSH_USER}@${SSH_HOST}:${REMOTE_ENV_FILE}"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "mkdir -p '$(dirname "$REMOTE_ENV_FILE")'"
scp "${SSH_OPTS[@]}" "$LOCAL_ENV_FILE" "${SSH_USER}@${SSH_HOST}:${REMOTE_ENV_FILE}"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" \
  "chown '${APP_USER}:www-data' '${REMOTE_ENV_FILE}' 2>/dev/null || true; chmod 0640 '${REMOTE_ENV_FILE}'"

echo "Remote env uploaded with mode 0640."
