# Production Deployment Plan (Budget + Longevity)

## Goal

Operate with a **local-first studio workflow** while publishing stable online works:

1. Local machine remains the primary place to build/test compositions and updates.
2. Finished compositions are hosted online with low-cost storage and fast delivery.
3. NFT media/metadata generation is reliable and owner mint flow stays current.
4. The system is designed for **decades of operation**, low monthly cost, and migration safety.

---

## Current State (from codebase)

- Django app already supports:
  - live composition pages
  - NFT preview/mint flows
  - generated square NFT image/video/metadata endpoints
- NFT settings are already environment-driven:
  - `NFT_ETH_CHAIN_ID`
  - `NFT_ETH_NETWORK_NAME`
  - `NFT_ETH_CONTRACT_ADDRESS`
  - `NFT_ETH_MARKETPLACE_BASE_URL`
- Storage currently defaults to local media with optional S3-compatible backend support.

This means we can keep app logic mostly unchanged and harden the deployment/storage layers.

---

## Recommended Architecture (Budget-First)

## 1) Environment Split

- **Local Studio (primary)**: your computer
  - composition creation, preview, admin, experimentation
  - schema and code changes validated here first
- **Production Online**
  - serves only published/finished works + mint pages
  - receives controlled deploys from local/staging

## 2) Hosting (avoid AWS, keep cost low)

- **App hosting**: Hetzner Cloud or OVH VPS (single VM to start)
  - 2 vCPU / 4 GB RAM is fine initially
- **Database**: managed Postgres (small plan) or self-hosted Postgres on VM
  - managed is safer for longevity/ops overhead
- **Reverse proxy + TLS**: Caddy or Nginx + automatic certificates
- **Domain**: `edfornieles.com` with Cloudflare DNS proxy enabled

## 3) Object Storage + CDN

- **Primary media/object storage**: Cloudflare R2 (S3-compatible, low egress cost)
- **Delivery**: Cloudflare CDN in front of app + media
- **Versioned buckets/prefixes**:
  - `compositions/live/`
  - `compositions/final/`
  - `nft/versions/{composition_id}/v{n}/`

## 4) Background Processing

- **Queue**: Redis + Celery worker
- Separate worker process for heavy video/image generation
- Keep web process responsive; generation never blocks user requests

## 5) NFT Contract Strategy

The full launch spec lives in [`NFT_LAUNCH_README.md`](NFT_LAUNCH_README.md).
Highlights:

- One production contract: `TheFeed` (ERC-721 + Enumerable + EIP-2981), 1000
  total supply, lazy Fisher-Yates random allocation, 0.05 / 0.07 / 0.10 ETH
  tiered pricing with a holder discount sourced from the Finiliar collection.
- Ethereum mainnet, owner = Gnosis Safe multisig, royalty = 7.00%.
- Foundry workspace at [`contracts/`](contracts/README.md) with a 42-test
  suite covering boundary pricing, the full-supply uniqueness invariant,
  reentrancy, refund failure, and three fuzz tests.
- Deploy is sepolia-soak first, then mainnet with provenance hash published
  before opening the sale; signing/minting stays wallet-driven from the
  frontend (`/mint/random/`).

---

## Deployment Topology (Phase 1)

- 1x VPS:
  - Django (gunicorn/uvicorn)
  - Redis
  - Celery worker
  - Celery beat (optional)
  - Caddy/Nginx
- 1x managed Postgres
- 1x R2 bucket (+ lifecycle rules + versioning)
- Cloudflare DNS/CDN/WAF

This is the minimum viable stable stack with low monthly burn.

---

## Budget Tiers (Monthly, Approx)

## Lean (recommended start)

- VPS: EUR10-20
- Managed Postgres small: EUR15-30
- R2 + CDN: EUR5-25 (depends on media volume)
- Monitoring/logging basic: EUR0-15
- **Total**: **EUR30-90 / month**

## Growth

- Larger VPS or split app/worker: EUR30-80
- Postgres medium: EUR30-80
- R2 + CDN higher traffic: EUR20-80
- Backups/monitoring: EUR15-40
- **Total**: **EUR95-280 / month**

## Exhibition/Heavy Throughput

- 2 app nodes + dedicated worker node(s): EUR120-300
- Postgres HA managed: EUR80-250
- Storage/CDN higher volume: EUR80-300
- Observability + alerting: EUR30-120
- **Total**: **EUR310-970 / month**

---

## Longevity Plan (Decades)

## Data durability rules

- Keep **3 copies** of critical artifacts:
  1. R2 primary
  2. periodic encrypted backup to second provider (Backblaze B2 or similar)
  3. quarterly cold archive snapshot (offline or glacier-like storage)

## Migration-proof formats

- Media: MP4/H.264 (+ optional mezzanine ProRes master)
- Images: PNG/JPEG/WebP
- Metadata: JSON with explicit schema version
- Keep hash manifests (`sha256`) for each minted artifact

## Operational continuity

- Infrastructure as code for deployment config
- One-command restore runbook tested every quarter
- Annual dependency refresh and migration drill
- Document key management and wallet recovery in offline procedure

---

## Security + Reliability Baseline

- Enforce HTTPS everywhere
- Private admin behind strong auth + IP allowlist where possible
- Rotate all secrets quarterly
- Daily DB backups + PITR if available
- Health checks and alerting:
  - web uptime
  - queue depth
  - failed NFT render jobs
  - storage errors

---

## Release Workflow (Local-First)

1. Build/test locally (your main environment).
2. Push to staging (optional but recommended).
3. Smoke test key flows:
   - composition render
   - mint page render
   - NFT package generation
4. Promote to production with tagged release.
5. Rollback path must be documented and tested.

---

## NFT Owner Flow (Target)

1. Owner opens composition mint page.
2. App checks ownership/eligibility policy.
3. App generates fresh NFT package (image/video/metadata) as new version.
4. Media/metadata saved to durable object storage path.
5. Frontend surfaces `tokenURI` and network/contract context.
6. Owner mints with wallet.
7. Mint tx hash is recorded and linked to version.

This aligns with your existing endpoints and adds operational hardening.

---

## Production Phases

## Phase 0 (1 week): Foundations

- Choose VPS provider + managed Postgres + R2 account
- Configure `edfornieles.com` DNS + TLS
- Add env-based prod settings and secret management

## Phase 1 (1-2 weeks): Publish Finished Works

- Deploy Django production stack
- Move media to R2 (S3-compatible backend)
- Put Cloudflare CDN in front
- Enable backups and baseline monitoring

## Phase 2 (1-2 weeks): NFT Hardening

- Move NFT generated artifacts to versioned durable storage paths
- Add queue-backed generation + retries + job visibility
- Add mint transaction recording and owner-facing status clarity

## Phase 3 (ongoing): Longevity Ops

- Quarterly restore drill
- Annual contract + dependency review
- Yearly archive export with integrity hashes

---

## Concrete Decisions to Make Now

1. Chain choice for primary minting:
   - Ethereum mainnet (prestige, higher gas)
   - Ethereum L2 (lower gas, still Ethereum ecosystem)
2. Storage policy:
   - keep only latest generated media vs keep all generated versions
3. Ownership rule:
   - any wallet can prepare package vs owner-only preparation
4. Service level target:
   - hobby-grade uptime vs exhibition-grade uptime

---

## Recommended Starting Stack (Exact)

- App: Django + gunicorn
- Queue: Celery + Redis
- DB: Managed Postgres
- Storage: Cloudflare R2 (S3 API)
- Edge/DNS: Cloudflare
- Host: Hetzner VPS
- Deploy: Git-based CI/CD
- Monitoring: uptime checks + error logging + queue alerts

This gives the best cost-to-longevity ratio and keeps migration options open.
