#!/usr/bin/env bash
set -euo pipefail

# Idempotent Hetzner Cloud provisioning:
# - creates/reuses SSH key in Hetzner
# - creates/reuses server
# - prints server IPv4 for deploy scripts
#
# Usage:
#   HETZNER_API_TOKEN=... \
#   HETZNER_SERVER_NAME=composition-prod \
#   HETZNER_LOCATION=nbg1 \
#   HETZNER_SERVER_TYPE=cpx21 \
#   HETZNER_IMAGE=ubuntu-24.04 \
#   SSH_PUBLIC_KEY_PATH=~/.ssh/id_ed25519.pub \
#   bash scripts/prod/provision_hetzner.sh

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required env var: ${name}" >&2
    exit 1
  fi
}

require_var HETZNER_API_TOKEN

HETZNER_API_BASE="${HETZNER_API_BASE:-https://api.hetzner.cloud/v1}"
HETZNER_SERVER_NAME="${HETZNER_SERVER_NAME:-composition-prod}"
HETZNER_LOCATION="${HETZNER_LOCATION:-nbg1}"
HETZNER_SERVER_TYPE="${HETZNER_SERVER_TYPE:-cpx21}"
HETZNER_IMAGE="${HETZNER_IMAGE:-ubuntu-24.04}"
HETZNER_SSH_KEY_NAME="${HETZNER_SSH_KEY_NAME:-$(whoami)-composition-key}"
SSH_PUBLIC_KEY_PATH="${SSH_PUBLIC_KEY_PATH:-$HOME/.ssh/id_ed25519.pub}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi
if [[ ! -f "$SSH_PUBLIC_KEY_PATH" ]]; then
  echo "SSH public key file not found: $SSH_PUBLIC_KEY_PATH" >&2
  exit 1
fi

api_request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local url="${HETZNER_API_BASE}${path}"
  if [[ -n "$body" ]]; then
    curl -fsS -X "$method" \
      -H "Authorization: Bearer ${HETZNER_API_TOKEN}" \
      -H "Content-Type: application/json" \
      "$url" \
      -d "$body"
  else
    curl -fsS -X "$method" \
      -H "Authorization: Bearer ${HETZNER_API_TOKEN}" \
      "$url"
  fi
}

json_get() {
  local expr="$1"
  python3 -c "import json,sys; data=json.load(sys.stdin); print(${expr})"
}

json_get_safestr() {
  local script="$1"
  python3 -c "import json,sys; data=json.load(sys.stdin); x=(${script}); print('' if x is None else x)"
}

echo "Checking Hetzner SSH key '${HETZNER_SSH_KEY_NAME}' ..."
ssh_keys_json="$(api_request GET "/ssh_keys")"
ssh_key_id="$(printf '%s' "$ssh_keys_json" | json_get_safestr "next((k.get('id') for k in data.get('ssh_keys', []) if k.get('name')=='${HETZNER_SSH_KEY_NAME}'), '')")"

if [[ -z "$ssh_key_id" ]]; then
  echo "Creating SSH key in Hetzner ..."
  pub_key_content="$(<"$SSH_PUBLIC_KEY_PATH")"
  create_ssh_key_body="$(python3 - <<PY
import json
print(json.dumps({
  "name": "${HETZNER_SSH_KEY_NAME}",
  "public_key": """${pub_key_content}"""
}))
PY
)"
  created_key_json="$(api_request POST "/ssh_keys" "$create_ssh_key_body")"
  ssh_key_id="$(printf '%s' "$created_key_json" | json_get "data.get('ssh_key', {}).get('id', '')")"
fi

if [[ -z "$ssh_key_id" ]]; then
  echo "Failed to resolve/create Hetzner SSH key." >&2
  exit 1
fi

echo "Checking server '${HETZNER_SERVER_NAME}' ..."
servers_json="$(api_request GET "/servers")"
server_id="$(printf '%s' "$servers_json" | json_get_safestr "next((s.get('id') for s in data.get('servers', []) if s.get('name')=='${HETZNER_SERVER_NAME}'), '')")"

if [[ -z "$server_id" ]]; then
  echo "Creating server '${HETZNER_SERVER_NAME}' ..."
  create_server_body="$(python3 - <<PY
import json
print(json.dumps({
  "name": "${HETZNER_SERVER_NAME}",
  "server_type": "${HETZNER_SERVER_TYPE}",
  "image": "${HETZNER_IMAGE}",
  "location": "${HETZNER_LOCATION}",
  "ssh_keys": [int("${ssh_key_id}")],
  "labels": {"app": "composition-webapp"}
}))
PY
)"
  created_server_json="$(api_request POST "/servers" "$create_server_body")"
  server_id="$(printf '%s' "$created_server_json" | json_get "data.get('server', {}).get('id', '')")"
fi

if [[ -z "$server_id" ]]; then
  echo "Failed to resolve/create server." >&2
  exit 1
fi

echo "Fetching server details ..."
server_json="$(api_request GET "/servers/${server_id}")"
server_ipv4="$(printf '%s' "$server_json" | json_get_safestr "data.get('server', {}).get('public_net', {}).get('ipv4', {}).get('ip', '')")"
server_status="$(printf '%s' "$server_json" | json_get_safestr "data.get('server', {}).get('status', '')")"

if [[ -z "$server_ipv4" ]]; then
  echo "Server created/found, but IPv4 is not available yet." >&2
  echo "SERVER_ID=${server_id}"
  exit 0
fi

echo "Provisioning complete."
echo "SERVER_ID=${server_id}"
echo "SERVER_NAME=${HETZNER_SERVER_NAME}"
echo "SERVER_STATUS=${server_status}"
echo "SERVER_IPV4=${server_ipv4}"
echo
echo "Suggested next step:"
echo "SSH_KEY_PATH=${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519} SSH_USER=root SSH_HOST=${server_ipv4} DOMAIN=${DOMAIN:-example.com} bash scripts/prod/deploy_hetzner.sh"

