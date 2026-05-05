# NFT Deploy Plan

*Last updated: 2026-05-04*

---

## Concept

Each composition is a **living artwork** — an animated canvas that continues to evolve. An NFT is a **checkpoint**: a permanent on-chain record pointing to a versioned snapshot (image + video) of the composition at a point in time. The live artwork continues after minting. This is architecturally honest and artistically intentional.

---

## Versioning model

- Every mint creates a new `CompositionNFT` record with an incrementing `version_number`
- Old version files are **never overwritten** — stored at `nft/versions/composition_{id}/v{n}/`
- The on-chain `tokenURI` points to `/nft/version/<id>/metadata.json` on our server
- We control what that endpoint returns — updating it updates what marketplaces display
- **Version history is public** — a version archive page shows all past snapshots with their videos
- NFT metadata surfaces `version_number`, `prepared_at` (version date), and `composition.updated_at`

---

## Three layers

```
LOCAL LAPTOP                    R2 (Cloudflare)                  PRODUCTION (Hetzner)
────────────────────            ────────────────────────         ────────────────────────────
Create compositions    ──────▶  sources/<bucket>/               Django app (same codebase)
Edit settings                   nft/versions/comp_N/v1/          Postgres DB
Generate NFT media     ──────▶  nft/versions/comp_N/v1/         Reads sources from R2
Quality control                   poster.jpg                     Serves live composition pages
Mark ready                        preview_15s.mp4                Serves public mint site
Push to prod           ──API──▶   collector_45s.mp4             /mint/ — browse & mint
                                  metadata.json                  /collect/<slug>/ — detail
                                                                 /collect/<slug>/versions/ — archive
```

---

## Local QC workflow (current state — mostly built)

1. Create composition, configure sources + settings
2. Mark `ready_for_deployment = True` via folder page or library
3. NFT Launchpad (`/nft/launchpad/`) — review all ready compositions
4. Generate media: poster (1080×1080 JPEG), 10s preview (720p MP4), 45s collector (1080p MP4)
5. Check all three media pips are green (no stale, no missing)
6. Preview mint page at `/mint/<slug>/` locally
7. Preview collect page at `/collect/<slug>/` locally
8. Review version archive section on collect page
9. When satisfied → push to production

---

## Export / sync

### Source folders → R2

When a composition is pushed to production, all associated source image folders are uploaded to R2:

```
R2 bucket: composition-sources
  sources/mask_anon_search/         ← source folders
  sources/family_portraits_search/
  nft/versions/composition_42/v1/   ← generated media
    poster.jpg
    preview_15s.mp4
    collector_45s.mp4
    metadata.json
```

Production reads source images from R2 (`USE_R2_STORAGE=True`). Local dev reads from `LOCAL_SOURCES_ROOT` on disk. The composition renderer is storage-agnostic.

### Management command: `push_composition`

```bash
# First push
python manage.py push_composition --id 42

# Update composition already on prod (settings edit)
python manage.py push_composition --id 42 --update

# Update + regenerate NFT videos on prod (new version)
python manage.py push_composition --id 42 --update --regen-videos
```

What it does:
1. Validates composition is `ready_for_deployment=True` with a public URL
2. Uploads source folders to R2 (skips files already present by hash)
3. Uploads generated media files to R2
4. POSTs composition data to production API endpoint
5. Reports success/failure per step

### Production API endpoint: `/api/admin/sync/`

- Authenticated via shared secret (`SYNC_SECRET` env var on both sides)
- Receives composition JSON payload
- Upserts composition record in Postgres
- Returns `{ok, composition_id, created}`

---

## Update workflow (composition already minted)

```
Edit locally  →  QC in Launchpad  →  push --update
                                         ↓
                                  Sources synced to R2
                                  Composition settings updated on prod
                                  Live iframe reflects edit immediately
                                         ↓ (optional)
                              push --update --regen-videos
                                         ↓
                              New CompositionNFT version created (v2, v3…)
                              New video files stored at nft/versions/comp_N/v2/
                              Metadata endpoint now returns v2 data
                              Old v1 files remain accessible forever
```

**NFT on-chain is unchanged.** Only the off-chain metadata (what the tokenURI returns) is updated. This is transparent and intentional — the composition is a living work.

---

## Version archive (public)

URL: `/collect/<slug>/versions/`  
Also shown as collapsible section on `/collect/<slug>/`

```
VERSION HISTORY

v3  · 12 Apr 2026  [current]
    ┌──────────────────┐
    │   [video: 10s]   │   [↓ Download collector 45s]
    └──────────────────┘
    Settings snapshot: strobe · mask_anon · 3 sources

v2  · 03 Feb 2026
    ┌──────────────────┐
    │   [video: 10s]   │   [↓ Download collector 45s]
    └──────────────────┘

v1  · 14 Jan 2026  [original mint]
    ┌──────────────────┐
    │   [video: 10s]   │
    └──────────────────┘
```

Each version served by existing `composition_nft_version_video` endpoint. Files in R2 are permanent.

---

## NFT metadata fields

Every version metadata response includes:

```json
{
  "name": "anon_me v3",
  "description": "...",
  "image": "https://.../poster.jpg",
  "animation_url": "https://.../preview_15s.mp4",
  "external_url": "https://yourdomain.com/anonme/",
  "attributes": [
    { "trait_type": "Version", "display_type": "number", "value": 3 },
    { "trait_type": "Version Date", "value": "2026-04-12" },
    { "trait_type": "Last Updated", "value": "2026-04-12" },
    { "trait_type": "Live URL", "value": "https://yourdomain.com/anonme/" },
    { "trait_type": "Mode", "value": "live" },
    { "trait_type": "Type", "value": "strobe" },
    { "trait_type": "Chain", "value": "Ethereum" }
  ]
}
```

---

## Production infrastructure

| Component | Choice | Notes |
|-----------|--------|-------|
| Server | Hetzner VPS (CX31 or higher — ffmpeg is CPU-heavy) | API token already configured |
| OS | Ubuntu 24.04 LTS | |
| Web server | nginx + gunicorn | Standard Django deploy |
| DB | Postgres 16 | SQLite locally, Postgres on prod |
| Media storage | Cloudflare R2 | Already configured, `USE_R2_STORAGE=True` on prod |
| SSL | Let's Encrypt / Certbot | |
| Codebase | Same Django repo | `DJANGO_SETTINGS_MODULE=djangoscrap.settings_prod` |
| Process manager | systemd | gunicorn + background workers |

---

## Build order

### Phase 1 — Foundation (no deploy needed)
- [x] NFT Launchpad (`/nft/launchpad/`)
- [x] Ready checkbox + bulk actions on folder page
- [ ] NFT metadata: add `Version Date` + `Last Updated` attributes
- [ ] Version archive section on `/collect/<slug>/`

### Phase 2 — R2 storage
- [ ] Ensure all NFT version files write to R2 (not local disk) when `USE_R2_STORAGE=True`
- [ ] Source folder upload to R2 (`push_composition` step 1)
- [ ] Verify composition renderer reads sources from R2 correctly

### Phase 3 — Push pipeline
- [ ] `push_composition` management command
- [ ] `/api/admin/sync/` production endpoint (authenticated)
- [ ] `--regen-videos` flag (triggers remote video generation)

### Phase 4 — Production deploy
- [ ] Hetzner VPS provisioning + nginx + gunicorn
- [ ] `settings_prod.py` (Postgres, R2, no DEBUG, proper SECRET_KEY)
- [ ] Systemd service files
- [ ] SSL via Let's Encrypt
- [ ] DNS configuration

### Phase 5 — Mint frontend
- [ ] Wallet connection (MetaMask / ethers.js) in mint templates
- [ ] Prepare + record-mint flow wired to contract
- [ ] OpenSea metadata refresh trigger after version update

---

## Key decisions made

- **Transparent versioning**: on-chain token unchanged, off-chain metadata updated. Version history public.
- **R2 for all media**: source folders + NFT files. No file sync between machines.
- **Same Django codebase**: local and prod run identical code, different settings.
- **No IPFS** (for now): metadata + media hosted on our server. Simpler to update. Can add IPFS pinning later if desired.
- **Postgres on prod, SQLite locally**: no local→prod DB sync needed beyond the push API.
