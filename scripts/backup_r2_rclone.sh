#!/usr/bin/env bash

# Backup Cloudflare R2 bucket contents to a local folder using rclone.
#
# How to run:
# 1) Install rclone: https://rclone.org/install/
# 2) Export required environment variables:
#      export R2_ACCOUNT_ID="your-account-id"
#      export R2_BUCKET_NAME="your-bucket-name"
#      export R2_ACCESS_KEY_ID="your-r2-access-key-id"
#      export R2_SECRET_ACCESS_KEY="your-r2-secret-access-key"
#    Optional:
#      export BACKUP_DIR="./backups/r2"
# 3) Run:
#      bash scripts/backup_r2_rclone.sh
#
# Notes:
# - BACKUP_DIR defaults to ./backups/r2
# - This script does NOT delete remote files.
# - A temporary rclone config is created automatically and removed on exit.

set -euo pipefail

log() {
  printf '[backup_r2_rclone] %s\n' "$1"
}

error_exit() {
  printf '[backup_r2_rclone] ERROR: %s\n' "$1" >&2
  exit 1
}

require_env() {
  local var_name="$1"
  if [[ -z "${!var_name:-}" ]]; then
    error_exit "Missing required environment variable: ${var_name}"
  fi
}

if ! command -v rclone >/dev/null 2>&1; then
  error_exit "rclone is not installed or not on PATH. Install it from https://rclone.org/install/"
fi

require_env "R2_ACCOUNT_ID"
require_env "R2_BUCKET_NAME"
require_env "R2_ACCESS_KEY_ID"
require_env "R2_SECRET_ACCESS_KEY"

BACKUP_DIR="${BACKUP_DIR:-./backups/r2}"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

log "Starting Cloudflare R2 backup with rclone..."
log "R2 endpoint: ${R2_ENDPOINT}"
log "Bucket: ${R2_BUCKET_NAME}"
log "Local backup dir: ${BACKUP_DIR}"

mkdir -p "${BACKUP_DIR}"
log "Ensured local backup directory exists."

TMP_RCLONE_CONFIG="$(mktemp "${TMPDIR:-/tmp}/rclone-r2-XXXXXX.conf")"
cleanup() {
  rm -f "${TMP_RCLONE_CONFIG}"
}
trap cleanup EXIT

cat > "${TMP_RCLONE_CONFIG}" <<EOF
[r2temp]
type = s3
provider = Cloudflare
access_key_id = ${R2_ACCESS_KEY_ID}
secret_access_key = ${R2_SECRET_ACCESS_KEY}
endpoint = ${R2_ENDPOINT}
acl = private
no_check_bucket = true
EOF

log "Temporary rclone config created."
log "Copying all files from r2temp:${R2_BUCKET_NAME} to ${BACKUP_DIR} (no remote deletes)..."
rclone copy "r2temp:${R2_BUCKET_NAME}" "${BACKUP_DIR}" \
  --config "${TMP_RCLONE_CONFIG}" \
  --progress

log "Backup complete."

