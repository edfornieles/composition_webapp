#!/usr/bin/env bash
set -euo pipefail

# Minimal post-deploy smoke checks for the public/admin MVP.
#
# Usage:
#   DOMAIN=edfornieles.com bash scripts/prod/smoke_check.sh

DOMAIN="${DOMAIN:-}"
BASE_URL="${BASE_URL:-}"

if [[ -z "$BASE_URL" ]]; then
  if [[ -z "$DOMAIN" ]]; then
    echo "Set DOMAIN or BASE_URL." >&2
    exit 1
  fi
  BASE_URL="https://${DOMAIN}"
fi

check_url() {
  local path="$1"
  local expected="${2:-200}"
  local url="${BASE_URL%/}${path}"
  local status
  status="$(curl -k -sS -o /dev/null -w "%{http_code}" "$url")"
  printf '%s -> %s\n' "$url" "$status"
  if [[ "$status" != "$expected" ]]; then
    echo "Unexpected status for ${url}: wanted ${expected}, got ${status}" >&2
    exit 1
  fi
}

check_url "/" "302"
check_url "/login/" "200"
check_url "/portfolio/" "200"

echo "Smoke checks passed."
