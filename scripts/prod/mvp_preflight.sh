#!/usr/bin/env bash
set -euo pipefail

# Read-only MVP deployment preflight.
#
# Usage:
#   DOMAIN=edfornieles.com bash scripts/prod/mvp_preflight.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

ok=1

check_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    printf 'ok   command: %s\n' "$cmd"
  else
    printf 'miss command: %s\n' "$cmd"
    ok=0
  fi
}

check_env() {
  local name="$1"
  local required="${2:-false}"
  if [[ -n "${!name:-}" ]]; then
    printf 'ok   env: %s\n' "$name"
  elif [[ "$required" == "true" ]]; then
    printf 'miss env: %s\n' "$name"
    ok=0
  else
    printf 'skip env: %s\n' "$name"
  fi
}

check_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    printf 'ok   file: %s\n' "$path"
  else
    printf 'miss file: %s\n' "$path"
    ok=0
  fi
}

echo "Local commands"
check_cmd python3
check_cmd curl
check_cmd rsync
check_cmd ssh
check_cmd scp
check_cmd rclone

echo ""
echo "Project files"
check_file requirements.txt
check_file manage.py
check_file deploy/hetzner/.env.production.example

echo ""
echo "Cloudflare/R2 env"
check_env CLOUDFLARE_API_TOKEN
check_env CLOUDFLARE_ACCOUNT_ID
check_env R2_ACCOUNT_ID
check_env R2_ACCESS_KEY_ID
check_env R2_SECRET_ACCESS_KEY

echo ""
echo "VPS env"
check_env HETZNER_API_TOKEN
check_env SSH_KEY_PATH
check_env SSH_USER
check_env SSH_HOST
check_env DOMAIN

echo ""
echo "Django check"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  "$ROOT_DIR/.venv/bin/python" manage.py check
else
  python3 manage.py check
fi

echo ""
if [[ "$ok" -eq 1 ]]; then
  echo "Preflight passed."
else
  echo "Preflight found missing optional/required deployment pieces. Fill them before running live provisioning/deploy."
fi
