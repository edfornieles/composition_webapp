#!/usr/bin/env bash

# Create standard Cloudflare R2 bucket structure for this project.
#
# How to run:
#   export R2_ACCOUNT_ID="your-account-id"
#   export R2_ACCESS_KEY_ID="your-r2-access-key-id"
#   export R2_SECRET_ACCESS_KEY="your-r2-secret-access-key"
#   bash scripts/r2_create_structure.sh
#
# Optional overrides:
#   export R2_BUCKETS="edfornieles-composition-prod edfornieles-composition-staging edfornieles-composition-archive"
#   export R2_PREFIXES="sources nft/versions renders backups"
#
# Notes:
# - Creates buckets if they don't exist.
# - Creates placeholder objects (.keep) for the standard prefixes.

set -euo pipefail

log() {
  printf '[r2_create_structure] %s\n' "$1"
}

error_exit() {
  printf '[r2_create_structure] ERROR: %s\n' "$1" >&2
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

R2_BUCKETS="${R2_BUCKETS:-edfornieles-composition-prod edfornieles-composition-staging edfornieles-composition-archive}"
R2_PREFIXES="${R2_PREFIXES:-sources nft/versions renders backups}"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

TMP_RCLONE_CONFIG="$(mktemp "${TMPDIR:-/tmp}/rclone-r2-structure-XXXXXX.conf")"
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
location_constraint = auto
acl = private
no_check_bucket = true
EOF

log "Using endpoint ${R2_ENDPOINT}"
log "Buckets to ensure: ${R2_BUCKETS}"
log "Prefixes to ensure: ${R2_PREFIXES}"

for bucket in ${R2_BUCKETS}; do
  log "Ensuring bucket exists: ${bucket}"
  if rclone lsd "r2:${bucket}" --config "${TMP_RCLONE_CONFIG}" >/dev/null 2>&1; then
    log "Bucket already exists: ${bucket}"
  else
    rclone mkdir "r2:${bucket}" --config "${TMP_RCLONE_CONFIG}"
    log "Created bucket: ${bucket}"
  fi

  for prefix in ${R2_PREFIXES}; do
    # Create explicit placeholder keys so prefix structure is visible immediately.
    keep_key="${prefix%/}/.keep"
    touched=0
    for _ in 1 2 3 4 5 6; do
      if rclone touch "r2:${bucket}/${keep_key}" --config "${TMP_RCLONE_CONFIG}" >/dev/null 2>&1; then
        touched=1
        break
      fi
      sleep 2
    done
    if [[ "${touched}" -ne 1 ]]; then
      error_exit "Failed to create prefix placeholder after retries: ${bucket}/${keep_key}"
    fi
    log "Ensured prefix ${bucket}/${prefix}/"
  done
done

log "R2 structure ready."

