#!/usr/bin/env bash
set -euo pipefail

# Publish the local MVP content state to production.
#
# This keeps composition/edit work local while pushing approved site data and
# assets online. It intentionally excludes auth/users by default so production
# admin accounts stay managed on the server.
#
# Required for remote DB load:
#   SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4
#
# Optional:
#   LOAD_REMOTE=false                  # Only create local fixture/R2 sync.
#   SYNC_R2=true                       # Sync local asset folders to R2.
#   R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
#   R2_BUCKET_NAME=edfornieles-composition-prod
#   APP_DIR=/srv/composition_webapp-main
#   APP_SERVICE_NAME=composition-webapp

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
EXPORT_DIR="${EXPORT_DIR:-$ROOT_DIR/exports/mvp-publish}"
FIXTURE_PATH="${FIXTURE_PATH:-$EXPORT_DIR/djangoscrap-public-$STAMP.json}"
APP_DIR="${APP_DIR:-/srv/composition_webapp-main}"
APP_SERVICE_NAME="${APP_SERVICE_NAME:-composition-webapp}"
REMOTE_FIXTURE_PATH="${REMOTE_FIXTURE_PATH:-$APP_DIR/tmp/djangoscrap-public-$STAMP.json}"
VENV_PATH="${VENV_PATH:-$APP_DIR/.venv}"
SSH_PORT="${SSH_PORT:-22}"
LOAD_REMOTE="${LOAD_REMOTE:-true}"
SYNC_R2="${SYNC_R2:-false}"
R2_BUCKET_NAME="${R2_BUCKET_NAME:-${R2_PROD_BUCKET:-edfornieles-composition-prod}}"

mkdir -p "$EXPORT_DIR"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

echo "Exporting local public MVP data to ${FIXTURE_PATH}"
"$PYTHON" manage.py dumpdata \
  djangoscrap.Series \
  djangoscrap.Composition \
  djangoscrap.CompositionRelease \
  djangoscrap.CompositionReleaseFile \
  djangoscrap.CompositionNFT \
  djangoscrap.CompositionMediaAsset \
  djangoscrap.MintCollectionSettings \
  djangoscrap.MonologuePersona \
  djangoscrap.AdvancedThoughtScenario \
  djangoscrap.WallProfile \
  --natural-foreign \
  --natural-primary \
  --indent 2 \
  -o "$FIXTURE_PATH"

if [[ "$SYNC_R2" == "true" ]]; then
  echo "Syncing local assets to R2 bucket ${R2_BUCKET_NAME}"
  bash "$ROOT_DIR/scripts/r2_create_structure.sh"
  if [[ -d "$ROOT_DIR/media" ]]; then
    bash "$ROOT_DIR/scripts/r2_publish_folder.sh" "$ROOT_DIR/media" "$R2_BUCKET_NAME" "media"
  fi
  if [[ -d "$ROOT_DIR/static/composition_thumbnails" ]]; then
    bash "$ROOT_DIR/scripts/r2_publish_folder.sh" "$ROOT_DIR/static/composition_thumbnails" "$R2_BUCKET_NAME" "static/composition_thumbnails"
  fi
  if [[ -d "$ROOT_DIR/compositions" ]]; then
    bash "$ROOT_DIR/scripts/r2_publish_folder.sh" "$ROOT_DIR/compositions" "$R2_BUCKET_NAME" "compositions"
  fi
fi

if [[ "$LOAD_REMOTE" != "true" ]]; then
  echo "Skipping remote load because LOAD_REMOTE=false."
  echo "Fixture created: ${FIXTURE_PATH}"
  exit 0
fi

for name in SSH_KEY_PATH SSH_USER SSH_HOST; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required env var for remote load: ${name}" >&2
    exit 1
  fi
done

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -p "$SSH_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

echo "Uploading fixture to ${SSH_USER}@${SSH_HOST}:${REMOTE_FIXTURE_PATH}"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "mkdir -p '$(dirname "$REMOTE_FIXTURE_PATH")'"
scp "${SSH_OPTS[@]}" "$FIXTURE_PATH" "${SSH_USER}@${SSH_HOST}:${REMOTE_FIXTURE_PATH}"

echo "Loading fixture and restarting production web service"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" \
  APP_DIR="$APP_DIR" \
  VENV_PATH="$VENV_PATH" \
  APP_SERVICE_NAME="$APP_SERVICE_NAME" \
  REMOTE_FIXTURE_PATH="$REMOTE_FIXTURE_PATH" \
  'bash -s' <<'REMOTE'
set -euo pipefail
cd "$APP_DIR"
"$VENV_PATH/bin/python" manage.py loaddata "$REMOTE_FIXTURE_PATH"
"$VENV_PATH/bin/python" manage.py collectstatic --noinput
sudo systemctl restart "${APP_SERVICE_NAME}-gunicorn.service"
REMOTE

echo "MVP publish completed."
