#!/usr/bin/env bash

# Backup Cloudflare R2 bucket contents to a local folder.
#
# How to run:
# 1) Ensure AWS CLI is installed and configured in your shell.
# 2) Export required environment variables:
#      export R2_ACCOUNT_ID="your-account-id"
#      export R2_BUCKET_NAME="your-bucket-name"
#      export AWS_ACCESS_KEY_ID="your-r2-access-key"
#      export AWS_SECRET_ACCESS_KEY="your-r2-secret-key"
#    Optional:
#      export BACKUP_DIR="/absolute/or/relative/path"
# 3) Run:
#      bash scripts/backup_r2.sh
#
# Notes:
# - BACKUP_DIR defaults to ./backups/r2
# - This script does NOT delete remote files.

set -euo pipefail

log() {
  printf '[backup_r2] %s\n' "$1"
}

error_exit() {
  printf '[backup_r2] ERROR: %s\n' "$1" >&2
  exit 1
}

require_env() {
  local var_name="$1"
  if [[ -z "${!var_name:-}" ]]; then
    error_exit "Missing required environment variable: ${var_name}"
  fi
}

require_env "R2_ACCOUNT_ID"
require_env "R2_BUCKET_NAME"
require_env "AWS_ACCESS_KEY_ID"
require_env "AWS_SECRET_ACCESS_KEY"

BACKUP_DIR="${BACKUP_DIR:-./backups/r2}"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
SOURCE_URI="s3://${R2_BUCKET_NAME}"

log "Starting Cloudflare R2 backup..."
log "R2 endpoint: ${R2_ENDPOINT}"
log "Bucket: ${R2_BUCKET_NAME}"
log "Local backup dir: ${BACKUP_DIR}"

mkdir -p "${BACKUP_DIR}"
log "Ensured local backup directory exists."

log "Syncing files from ${SOURCE_URI} to ${BACKUP_DIR} (download-only, no remote deletes)..."
aws s3 sync "${SOURCE_URI}" "${BACKUP_DIR}" \
  --endpoint-url "${R2_ENDPOINT}" \
  --only-show-errors

log "Backup complete."

