#!/usr/bin/env bash

# Publish/update one local source folder to Cloudflare R2 without deleting remote files.
#
# How to run:
#   export R2_ACCOUNT_ID="your-account-id"
#   export R2_ACCESS_KEY_ID="your-r2-access-key-id"
#   export R2_SECRET_ACCESS_KEY="your-r2-secret-access-key"
#   bash scripts/r2_publish_folder.sh "/path/to/local/folder" "edfornieles-composition-prod" "sources/my-folder"
#
# Dry-run:
#   DRY_RUN=true bash scripts/r2_publish_folder.sh "/path/to/local/folder" "bucket" "sources/my-folder"

set -euo pipefail

log() {
  printf '[r2_publish_folder] %s\n' "$1"
}

error_exit() {
  printf '[r2_publish_folder] ERROR: %s\n' "$1" >&2
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
  error_exit "Usage: bash scripts/r2_publish_folder.sh <local_dir> <bucket> <remote_prefix>"
fi
if [[ ! -d "${LOCAL_DIR}" ]]; then
  error_exit "Local folder does not exist: ${LOCAL_DIR}"
fi

DRY_RUN="${DRY_RUN:-false}"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
REMOTE_PATH="r2:${BUCKET}/${REMOTE_PREFIX%/}"

TMP_RCLONE_CONFIG="$(mktemp "${TMPDIR:-/tmp}/rclone-r2-publish-XXXXXX.conf")"
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

log "Publishing local folder to R2"
log "Local:  ${LOCAL_DIR}"
log "Remote: ${REMOTE_PATH}"
log "Mode:   $( [[ "${DRY_RUN}" == "true" ]] && echo "dry-run" || echo "live copy" )"

EXTRA_ARGS=()
if [[ "${DRY_RUN}" == "true" ]]; then
  EXTRA_ARGS+=(--dry-run)
fi

rclone copy "${LOCAL_DIR}" "${REMOTE_PATH}" \
  --config "${TMP_RCLONE_CONFIG}" \
  --progress \
  "${EXTRA_ARGS[@]}"

log "Publish completed."

