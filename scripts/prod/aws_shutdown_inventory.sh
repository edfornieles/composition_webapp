#!/usr/bin/env bash
set -euo pipefail

# Read-only AWS shutdown inventory.
#
# This does not delete or modify anything. It writes JSON reports for the
# common billable services that should be checked before closing down AWS use.
#
# Usage:
#   bash scripts/prod/aws_shutdown_inventory.sh
#
# Optional:
#   OUT_DIR=aws-shutdown-inventory/20260427
#   AWS_REGIONS="us-east-1 eu-west-2"

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required." >&2
  exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OUT_DIR:-aws-shutdown-inventory/$STAMP}"
mkdir -p "$OUT_DIR"

run_json() {
  local outfile="$1"
  shift
  echo "Exporting ${outfile}"
  if "$@" --output json >"$OUT_DIR/${outfile}.json" 2>"$OUT_DIR/${outfile}.err"; then
    rm -f "$OUT_DIR/${outfile}.err"
  else
    echo "Warning: failed ${outfile}; see ${OUT_DIR}/${outfile}.err" >&2
  fi
}

echo "Checking AWS caller identity ..."
run_json "sts-caller-identity" aws sts get-caller-identity

if [[ -z "${AWS_REGIONS:-}" ]]; then
  if regions_json="$(aws ec2 describe-regions --all-regions --output json 2>/dev/null)"; then
    AWS_REGIONS="$(printf '%s' "$regions_json" | python3 -c 'import json,sys; print(" ".join(r["RegionName"] for r in json.load(sys.stdin).get("Regions", []) if r.get("OptInStatus") in (None, "opt-in-not-required", "opted-in")))')"
  else
    AWS_REGIONS="us-east-1"
  fi
fi

echo "Regions: ${AWS_REGIONS}"

run_json "route53-hosted-zones" aws route53 list-hosted-zones
run_json "route53domains-domains" aws route53domains list-domains --region us-east-1
run_json "s3-buckets" aws s3api list-buckets
run_json "cloudfront-distributions" aws cloudfront list-distributions
run_json "iam-users" aws iam list-users
run_json "iam-access-keys" aws iam list-access-keys

for region in ${AWS_REGIONS}; do
  safe_region="${region//[^a-zA-Z0-9-]/_}"
  run_json "ec2-instances-${safe_region}" aws ec2 describe-instances --region "$region"
  run_json "ec2-addresses-${safe_region}" aws ec2 describe-addresses --region "$region"
  run_json "ec2-volumes-${safe_region}" aws ec2 describe-volumes --region "$region"
  run_json "ec2-snapshots-owned-${safe_region}" aws ec2 describe-snapshots --owner-ids self --region "$region"
  run_json "rds-instances-${safe_region}" aws rds describe-db-instances --region "$region"
  run_json "elasticache-clusters-${safe_region}" aws elasticache describe-cache-clusters --region "$region"
  run_json "lambda-functions-${safe_region}" aws lambda list-functions --region "$region"
  run_json "cloudwatch-log-groups-${safe_region}" aws logs describe-log-groups --region "$region"
done

cat >"$OUT_DIR/README.txt" <<EOF
AWS shutdown inventory created at ${STAMP}.

Review these files before deleting anything:
- route53-hosted-zones.json and route53domains-domains.json
- ec2-instances-*.json, ec2-addresses-*.json, ec2-volumes-*.json
- rds-instances-*.json and elasticache-clusters-*.json
- s3-buckets.json
- cloudfront-distributions.json
- iam-users.json and iam-access-keys.json
- cloudwatch-log-groups-*.json

Delete hosted zones only after Cloudflare DNS is active and verified.
Release Elastic IPs only after no live server depends on them.
Keep S3/RDS/snapshot backups until the new MVP has been verified.
EOF

echo ""
echo "Inventory complete: ${OUT_DIR}"
