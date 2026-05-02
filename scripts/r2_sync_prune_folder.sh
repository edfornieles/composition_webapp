#!/usr/bin/env bash

# Sync one local source folder to Cloudflare R2 and prune remote extras.
# Remote deletions are safety-routed to a backup prefix.
#
# How to run:
#   export R2_ACCOUNT_ID="your-account-id"
#   export R2_ACCESS_KEY_ID="your-r2-access-key-id"
#   export R2_SECRET_ACCESS_KEY="your-r2-secret-access-key"
#   bash scripts/r2_sync_prune_folder.sh "/path/to/local/folder" "edfornieles-composition-prod" "sources/my-folder"
#
# Optional:
#   export PRUNE_BACKUP_PREFIX="_deleted"
#   export DRY_RUN=true

set -euo pipefail

log() {
  printf '[r2_sync_prune_folder] %s\n' "$1"
}

error_exit() {
  printf '[r2_sync_prune_folder] ERROR: %s\n' "$1" >&2
  exit 1
}

require_env() {
  local var_name="$1"
  if [[ -z "${!var_name:-}" ]]; then
    error_exit "Missing required environment variable: ${var_name}"
  fi
}

if ! command -v rclone >/dev/null 2>&1; then
  error_exit "rclone is not installed or not on PATH."
fi

require_env "R2_ACCOUNT_ID"
require_env "R2_ACCESS_KEY_ID"
require_env "R2_SECRET_ACCESS_KEY"

LOCAL_DIR="${1:-}"
BUCKET="${2:-}"
REMOTE_PREFIX="${3:-}"

if [[ -z "${LOCAL_DIR}" || -z "${BUCKET}" || -z "${REMOTE_PREFIX}" ]]; then
  error_exit "Usage: bash scripts/r2_sync_prune_folder.sh <local_dir> <bucket> <remote_prefix>"
fi
if [[ ! -d "${LOCAL_DIR}" ]]; then
  error_exit "Local folder does not exist: ${LOCAL_DIR}"
fi

PRUNE_BACKUP_PREFIX="${PRUNE_BACKUP_PREFIX:-_deleted}"
DRY_RUN="${DRY_RUN:-false}"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
REMOTE_PATH="r2:${BUCKET}/${REMOTE_PREFIX%/}"
BACKUP_DIR_REMOTE="r2:${BUCKET}/${PRUNE_BACKUP_PREFIX%/}/${REMOTE_PREFIX%/}/${STAMP}"

TMP_RCLONE_CONFIG="$(mktemp "${TMPDIR:-/tmp}/rclone-r2-sync-XXXXXX.conf")"
cleanup() {
  rm -f "${TMP_RCLONE_CONFIG}"
}
trap cleanup EXIT

cat > "${TMP_RCLONE_CONFIG}" <<EOF
[r2]
type = s3
provider = Cloudflare
access_key_id = ${R2_ACCESS_KEY_ID}
secret_access_key = ${R2_SECRET_ACCESS_KEY}
endpoint = ${R2_ENDPOINT}
acl = private
no_check_bucket = true
EOF

log "Sync+prune local folder to R2"
log "Local:         ${LOCAL_DIR}"
log "Remote:        ${REMOTE_PATH}"
log "Prune backup:  ${BACKUP_DIR_REMOTE}"
log "Mode:          $( [[ "${DRY_RUN}" == "true" ]] && echo "dry-run" || echo "live sync" )"

EXTRA_ARGS=()
if [[ "${DRY_RUN}" == "true" ]]; then
  EXTRA_ARGS+=(--dry-run)
fi

rclone sync "${LOCAL_DIR}" "${REMOTE_PATH}" \
  --config "${TMP_RCLONE_CONFIG}" \
  --backup-dir "${BACKUP_DIR_REMOTE}" \
  --progress \
  "${EXTRA_ARGS[@]}"

log "Sync+prune completed."

