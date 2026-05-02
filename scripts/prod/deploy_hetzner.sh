#!/usr/bin/env bash
set -euo pipefail

# Idempotent production deployment to Hetzner Ubuntu.
# Usage:
#   SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 DOMAIN=edfornieles.com \
#   APP_DIR=/srv/composition_webapp-main bash scripts/prod/deploy_hetzner.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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
require_var DOMAIN

SSH_PORT="${SSH_PORT:-22}"
APP_DIR="${APP_DIR:-/srv/composition_webapp-main}"
APP_SERVICE_NAME="${APP_SERVICE_NAME:-composition-webapp}"
APP_USER="${APP_USER:-$SSH_USER}"
ENV_FILE="${ENV_FILE:-$APP_DIR/djangoscrap/.env}"
VENV_PATH="${VENV_PATH:-$APP_DIR/.venv}"
DJANGO_WSGI_MODULE="${DJANGO_WSGI_MODULE:-djangoscrap.wsgi:application}"
GUNICORN_BIND="${GUNICORN_BIND:-127.0.0.1:8001}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-180}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-1}"
RUN_CELERY="${RUN_CELERY:-true}"

if [[ ! -f "$SSH_KEY_PATH" ]]; then
  echo "SSH key not found at SSH_KEY_PATH: $SSH_KEY_PATH" >&2
  exit 1
fi

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -p "$SSH_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

echo "Syncing project files to ${SSH_USER}@${SSH_HOST}:${APP_DIR} ..."
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "mkdir -p '$APP_DIR'"
rsync -az --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude ".git/" \
  --exclude ".venv/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "node_modules/" \
  --exclude "db.sqlite3" \
  --exclude "staticfiles/" \
  --exclude "media/" \
  "$ROOT_DIR/" "${SSH_USER}@${SSH_HOST}:$APP_DIR/"

echo "Configuring server packages, services, and app runtime ..."
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" \
  APP_DIR="$APP_DIR" \
  APP_SERVICE_NAME="$APP_SERVICE_NAME" \
  APP_USER="$APP_USER" \
  ENV_FILE="$ENV_FILE" \
  VENV_PATH="$VENV_PATH" \
  DJANGO_WSGI_MODULE="$DJANGO_WSGI_MODULE" \
  GUNICORN_BIND="$GUNICORN_BIND" \
  GUNICORN_WORKERS="$GUNICORN_WORKERS" \
  GUNICORN_TIMEOUT="$GUNICORN_TIMEOUT" \
  CELERY_CONCURRENCY="$CELERY_CONCURRENCY" \
  RUN_CELERY="$RUN_CELERY" \
  DOMAIN="$DOMAIN" \
  'bash -s' <<'REMOTE'
set -euo pipefail

sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip \
  build-essential pkg-config libmysqlclient-dev \
  nginx redis-server ffmpeg rsync sqlite3

sudo systemctl enable redis-server
sudo systemctl start redis-server

sudo mkdir -p "$APP_DIR"
sudo chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Warning: $ENV_FILE not found. Create it before first production run." >&2
fi

if [[ ! -x "$VENV_PATH/bin/python" ]]; then
  python3 -m venv "$VENV_PATH"
fi

"$VENV_PATH/bin/pip" install --upgrade pip wheel
"$VENV_PATH/bin/pip" install -r "$APP_DIR/requirements.txt"

render_tpl() {
  local src="$1"
  local dst="$2"
  sed \
    -e "s|%APP_DIR%|$APP_DIR|g" \
    -e "s|%APP_SERVICE_NAME%|$APP_SERVICE_NAME|g" \
    -e "s|%APP_USER%|$APP_USER|g" \
    -e "s|%ENV_FILE%|$ENV_FILE|g" \
    -e "s|%VENV_PATH%|$VENV_PATH|g" \
    -e "s|%DJANGO_WSGI_MODULE%|$DJANGO_WSGI_MODULE|g" \
    -e "s|%GUNICORN_BIND%|$GUNICORN_BIND|g" \
    -e "s|%GUNICORN_WORKERS%|$GUNICORN_WORKERS|g" \
    -e "s|%GUNICORN_TIMEOUT%|$GUNICORN_TIMEOUT|g" \
    -e "s|%CELERY_CONCURRENCY%|$CELERY_CONCURRENCY|g" \
    -e "s|%DOMAIN%|$DOMAIN|g" \
    "$src" | sudo tee "$dst" >/dev/null
}

render_tpl "$APP_DIR/deploy/hetzner/systemd/gunicorn.service.tpl" "/etc/systemd/system/${APP_SERVICE_NAME}-gunicorn.service"
render_tpl "$APP_DIR/deploy/hetzner/nginx/site.conf.tpl" "/etc/nginx/sites-available/${APP_SERVICE_NAME}.conf"
if [[ "$RUN_CELERY" == "true" ]]; then
  render_tpl "$APP_DIR/deploy/hetzner/systemd/celery.service.tpl" "/etc/systemd/system/${APP_SERVICE_NAME}-celery.service"
fi

sudo ln -sfn "/etc/nginx/sites-available/${APP_SERVICE_NAME}.conf" "/etc/nginx/sites-enabled/${APP_SERVICE_NAME}.conf"
if [[ -L /etc/nginx/sites-enabled/default ]]; then
  sudo rm -f /etc/nginx/sites-enabled/default
fi

sudo systemctl daemon-reload
sudo nginx -t

cd "$APP_DIR"
"$VENV_PATH/bin/python" manage.py migrate --noinput
"$VENV_PATH/bin/python" manage.py collectstatic --noinput
"$VENV_PATH/bin/python" manage.py check

sudo systemctl enable "${APP_SERVICE_NAME}-gunicorn.service"
sudo systemctl restart "${APP_SERVICE_NAME}-gunicorn.service"
if [[ "$RUN_CELERY" == "true" ]]; then
  sudo systemctl enable "${APP_SERVICE_NAME}-celery.service"
  sudo systemctl restart "${APP_SERVICE_NAME}-celery.service"
else
  sudo systemctl disable --now "${APP_SERVICE_NAME}-celery.service" 2>/dev/null || true
fi
sudo systemctl restart nginx

echo "Deployment completed."
REMOTE

if [[ "$RUN_CELERY" == "true" ]]; then
  echo "Done. Services: ${APP_SERVICE_NAME}-gunicorn, ${APP_SERVICE_NAME}-celery, nginx, redis-server"
else
  echo "Done. Services: ${APP_SERVICE_NAME}-gunicorn, nginx, redis-server"
fi

