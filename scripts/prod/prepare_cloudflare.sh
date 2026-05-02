#!/usr/bin/env bash
set -euo pipefail

# Create/reuse Cloudflare DNS zone records and an R2 bucket for the MVP.
#
# Required:
#   CLOUDFLARE_API_TOKEN=...
#   DOMAIN=edfornieles.com
#   SERVER_IPV4=1.2.3.4
#
# Optional:
#   CLOUDFLARE_ACCOUNT_ID=...       # Required for R2 bucket creation.
#   R2_BUCKET_NAME=edfornieles-composition-prod
#   WWW_RECORD_MODE=cname           # cname or a
#   CLOUDFLARE_PROXY=true           # proxy public web records through Cloudflare.

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required env var: ${name}" >&2
    exit 1
  fi
}

require_var CLOUDFLARE_API_TOKEN
require_var DOMAIN
require_var SERVER_IPV4

CF_API_BASE="${CF_API_BASE:-https://api.cloudflare.com/client/v4}"
CLOUDFLARE_PROXY="${CLOUDFLARE_PROXY:-true}"
WWW_RECORD_MODE="${WWW_RECORD_MODE:-cname}"
R2_BUCKET_NAME="${R2_BUCKET_NAME:-${DOMAIN//./-}-composition-prod}"

api_request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local url="${CF_API_BASE}${path}"
  if [[ -n "$body" ]]; then
    curl -fsS -X "$method" \
      -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
      -H "Content-Type: application/json" \
      "$url" \
      -d "$body"
  else
    curl -fsS -X "$method" \
      -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
      "$url"
  fi
}

json_get() {
  local expr="$1"
  python3 -c "import json,sys; data=json.load(sys.stdin); print(${expr})"
}

json_body() {
  python3 - "$@" <<'PY'
import json
import sys
import re

pairs = [arg.split("=", 1) for arg in sys.argv[1:]]
payload = {}
for key, value in pairs:
    if value == "true":
        payload[key] = True
    elif value == "false":
        payload[key] = False
    elif re.fullmatch(r"\d+", value):
        payload[key] = int(value)
    else:
        payload[key] = value
print(json.dumps(payload))
PY
}

echo "Checking Cloudflare zone for ${DOMAIN} ..."
zones_json="$(api_request GET "/zones?name=${DOMAIN}")"
zone_id="$(printf '%s' "$zones_json" | json_get "next((z.get('id') for z in data.get('result', []) if z.get('name') == '${DOMAIN}'), '')")"

if [[ -z "$zone_id" ]]; then
  echo "Creating Cloudflare zone ${DOMAIN} ..."
  create_zone_body="$(json_body "name=${DOMAIN}" "type=full")"
  zone_json="$(api_request POST "/zones" "$create_zone_body")"
  zone_id="$(printf '%s' "$zone_json" | json_get "data.get('result', {}).get('id', '')")"
fi

if [[ -z "$zone_id" ]]; then
  echo "Could not resolve Cloudflare zone id for ${DOMAIN}." >&2
  exit 1
fi

upsert_dns_record() {
  local record_type="$1"
  local record_name="$2"
  local record_content="$3"
  local proxied="$4"

  local records_json record_id body
  records_json="$(api_request GET "/zones/${zone_id}/dns_records?type=${record_type}&name=${record_name}")"
  record_id="$(printf '%s' "$records_json" | json_get "next((r.get('id') for r in data.get('result', []) if r.get('name') == '${record_name}' and r.get('type') == '${record_type}'), '')")"
  body="$(json_body "type=${record_type}" "name=${record_name}" "content=${record_content}" "ttl=1" "proxied=${proxied}")"

  if [[ -n "$record_id" ]]; then
    echo "Updating DNS ${record_type} ${record_name} -> ${record_content}"
    api_request PATCH "/zones/${zone_id}/dns_records/${record_id}" "$body" >/dev/null
  else
    echo "Creating DNS ${record_type} ${record_name} -> ${record_content}"
    api_request POST "/zones/${zone_id}/dns_records" "$body" >/dev/null
  fi
}

upsert_dns_record "A" "$DOMAIN" "$SERVER_IPV4" "$CLOUDFLARE_PROXY"
if [[ "$WWW_RECORD_MODE" == "a" ]]; then
  upsert_dns_record "A" "www.${DOMAIN}" "$SERVER_IPV4" "$CLOUDFLARE_PROXY"
else
  upsert_dns_record "CNAME" "www.${DOMAIN}" "$DOMAIN" "$CLOUDFLARE_PROXY"
fi

if [[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]]; then
  echo "Creating/reusing R2 bucket ${R2_BUCKET_NAME} ..."
  api_request PUT "/accounts/${CLOUDFLARE_ACCOUNT_ID}/r2/buckets/${R2_BUCKET_NAME}" "{}" >/dev/null || {
    echo "Warning: R2 bucket create failed. Check token permissions or whether the bucket already exists." >&2
  }
else
  echo "Skipping R2 bucket creation because CLOUDFLARE_ACCOUNT_ID is not set."
fi

zone_json="$(api_request GET "/zones/${zone_id}")"
printf '%s' "$zone_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
result = data.get("result", {})
print("")
print("Cloudflare setup complete.")
print(f"ZONE_ID={result.get('id', '')}")
print("Nameservers:")
for ns in result.get("name_servers", []):
    print(f"- {ns}")
'

echo ""
echo "Use these values in production env:"
echo "ALLOWED_HOSTS=${DOMAIN},www.${DOMAIN}"
echo "CSRF_TRUSTED_ORIGINS=https://${DOMAIN},https://www.${DOMAIN}"
echo "R2_BUCKET_NAME=${R2_BUCKET_NAME}"
