#!/usr/bin/env bash
set -euo pipefail

# Route 53 to Cloudflare migration helper.
#
# This script is safe by default: it exports AWS DNS/registration state and
# prints next steps. It changes nameservers only when APPLY_NAMESERVERS=true.
#
# Usage:
#   DOMAIN=edfornieles.com bash scripts/prod/route53_to_cloudflare.sh
#
# Optional:
#   CLOUDFLARE_NAMESERVERS="ada.ns.cloudflare.com,bob.ns.cloudflare.com"
#   APPLY_NAMESERVERS=true
#   WRITE_AUTH_CODE=true

DOMAIN="${DOMAIN:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"
OUT_DIR="${OUT_DIR:-route53-cloudflare-migration/${DOMAIN}}"
CLOUDFLARE_NAMESERVERS="${CLOUDFLARE_NAMESERVERS:-}"
APPLY_NAMESERVERS="${APPLY_NAMESERVERS:-false}"
WRITE_AUTH_CODE="${WRITE_AUTH_CODE:-false}"

if [[ -z "$DOMAIN" ]]; then
  echo "Set DOMAIN." >&2
  exit 1
fi
if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

echo "Checking AWS caller identity ..."
aws sts get-caller-identity --output json >"$OUT_DIR/aws-caller-identity.json"

echo "Exporting Route 53 hosted zones ..."
aws route53 list-hosted-zones --output json >"$OUT_DIR/hosted-zones.json"
hosted_zone_id="$(
  DOMAIN="$DOMAIN" python3 - "$OUT_DIR/hosted-zones.json" <<'PY'
import json
import os
import sys

domain = os.environ["DOMAIN"].rstrip(".") + "."
with open(sys.argv[1]) as fh:
    zones = json.load(fh).get("HostedZones", [])
for zone in zones:
    if zone.get("Name") == domain:
        print(zone.get("Id", "").split("/")[-1])
        break
PY
)"

if [[ -n "$hosted_zone_id" ]]; then
  echo "Exporting records for hosted zone ${hosted_zone_id} ..."
  aws route53 list-resource-record-sets \
    --hosted-zone-id "$hosted_zone_id" \
    --output json >"$OUT_DIR/resource-record-sets.json"
else
  echo "No matching Route 53 hosted zone found for ${DOMAIN}."
fi

echo "Exporting registered domain details ..."
if aws route53domains get-domain-detail \
  --region "$AWS_REGION" \
  --domain-name "$DOMAIN" \
  --output json >"$OUT_DIR/domain-detail.json"; then
  echo "Domain registration found in Route 53."
else
  echo "Route 53 domain registration not found or unavailable for ${DOMAIN}." >&2
fi

if [[ "$WRITE_AUTH_CODE" == "true" ]]; then
  echo "Retrieving transfer auth code into ${OUT_DIR}/transfer-auth-code.json"
  aws route53domains retrieve-domain-auth-code \
    --region "$AWS_REGION" \
    --domain-name "$DOMAIN" \
    --output json >"$OUT_DIR/transfer-auth-code.json"
  chmod 0600 "$OUT_DIR/transfer-auth-code.json"
fi

if [[ -n "$CLOUDFLARE_NAMESERVERS" ]]; then
  nameserver_json="$(
    CLOUDFLARE_NAMESERVERS="$CLOUDFLARE_NAMESERVERS" python3 - <<'PY'
import json
import os

servers = [s.strip() for s in os.environ["CLOUDFLARE_NAMESERVERS"].split(",") if s.strip()]
print(json.dumps({"Nameservers": [{"Name": server} for server in servers]}))
PY
  )"
  printf '%s\n' "$nameserver_json" >"$OUT_DIR/cloudflare-nameservers.json"

  if [[ "$APPLY_NAMESERVERS" == "true" ]]; then
    echo "Applying Cloudflare nameservers to Route 53 registration ..."
    aws route53domains update-domain-nameservers \
      --region "$AWS_REGION" \
      --domain-name "$DOMAIN" \
      --cli-input-json "file://$OUT_DIR/cloudflare-nameservers.json"
  else
    echo "Dry run: set APPLY_NAMESERVERS=true to update registrar nameservers."
  fi
else
  echo "Set CLOUDFLARE_NAMESERVERS after Cloudflare assigns them, then rerun to prepare/apply registrar nameserver change."
fi

echo ""
echo "Migration export complete: ${OUT_DIR}"
echo "Next: verify DNS records in Cloudflare, disable DNSSEC if enabled, change nameservers, then start registrar transfer."
