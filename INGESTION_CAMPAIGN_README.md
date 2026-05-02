# Autonomous Ingestion Campaigns

Last updated: April 2026

Scope note: this document is ingestion-only.  
For gallery wall orchestration and 9-screen playback runtime, use:

- `README.md` (runtime endpoints and setup)
- `BIG_PICTURE_README.md` (installation/product direction)

The ingestion campaign system is an always-on sourcing machine for the
composition webapp. Give it a folder concept (e.g. "80s cyberpunk neon
photography") and it autonomously discovers, downloads, filters, and
aesthetically curates assets into a named source folder — refreshing
and expanding that folder over time as new matching content appears on
the web.

**Target UX**: describe the folder you want. The system does the rest and
keeps it fresh.

**Implementation plan**: step-by-step checklist lives in
[`INGESTION_IMPLEMENTATION_PLAN.md`](./INGESTION_IMPLEMENTATION_PLAN.md).
Open that file first when resuming work on the roadmap.

---

## North star

> I want to say the types of folders and images I want and it just does
> them — and updates old ones with new images when necessary.

Concretely that means the system must:

1. **Source across every surface** — search engines, social media,
   booru/tag boards, reverse-image-search, full account archives.
2. **Be aesthetic-aware** — find images that share visual continuity with
   an example, not just images that match a text query.
3. **Handle every media kind** — JPEG/PNG/WebP/GIF/AVIF, video (MP4/MOV/
   WebM), animated GIFs, live photos.
4. **Be efficient** — drop junk before it's downloaded; upgrade thumbnails
   to full-res; throttle politely so we're never banned.
5. **Be incremental** — re-runs pick up only what's new since last sync.
6. **Be fast** — browser-refresh-level feedback on batch state.

---

## Architecture (as-of April 2026)

```text
              ┌──────────────────────────────┐
              │  Campaign definition         │
              │  - concept brief             │
              │  - AI-generated query pack   │
              │  - CBIR seed images (opt.)   │
              │  - vision-QC preferences     │
              │  - target asset count        │
              └──────────────┬───────────────┘
                             │
          ┌──────────────────┴──────────────────┐
          │  Extraction fan-out                 │
          │  (djangoscrap.views._extract_candi… │
          │                                     │
          │  engines attempted per page:        │
          │    Yandex-HTML  │ gallery-dl        │
          │    Playwright   │ yt-dlp            │
          │    instaloader  │ (future:          │
          │                 │  instagrapi,      │
          │                 │  Pinterest/Tumblr │
          │                 │  scroll-harvest)  │
          └──────────────┬──────────────────────┘
                         │ list[str] of URLs
                         ▼
          ┌──────────────────────────────────┐
          │  URL rewriter                    │
          │  djangoscrap.ingestion_url_rew…  │
          │                                  │
          │  - thumbnail → /originals/       │
          │  - watermark-host ignore list    │
          │  - dedupe on post-rewrite URL    │
          │  - JSON overlay for live tuning  │
          └──────────────┬───────────────────┘
                         │ upgraded+deduped URLs
                         ▼
          ┌──────────────────────────────────┐
          │  Polite HTTP layer               │
          │  djangoscrap.ingestion_http      │
          │                                  │
          │  - HEAD probe (size + type gate) │
          │  - per-host semaphore            │
          │  - min-delay between requests    │
          │  - download + write-to-disk      │
          └──────────────┬───────────────────┘
                         │ file on disk + SHA/pHash/CLIP
                         ▼
          ┌──────────────────────────────────┐
          │  Three-layer dedupe              │
          │  (views._detect_duplicate)       │
          │                                  │
          │  1. SHA256      — exact bytes    │
          │  2. pHash       — near-dup       │
          │  3. CLIP cosine — semantic       │
          └──────────────┬───────────────────┘
                         │ novel items only
                         ▼
          ┌──────────────────────────────────┐
          │  Vision-QC (GPT-4o-mini)         │
          │  - concept-brief match           │
          │  - off-topic rejections          │
          │    delete file, keep row as      │
          │    dedupe signal only            │
          └──────────────┬───────────────────┘
                         │ accepted items
                         ▼
              ┌────────────────────────┐
              │  Ingestion batch grid  │
              │  /ingestion/<id>/      │
              │  accept / reject / QC  │
              │  → publish to source   │
              └────────────────────────┘
```text

### Modules at a glance

| Module | Role |
| --- | --- |
| `djangoscrap/views.py::_run_campaign_cycle` | Orchestrator. One "tick" of an autonomous campaign. |
| `djangoscrap/ingestion_url_rewriter.py` | Per-host URL upgrade/ignore rules + JSON overlay. |
| `djangoscrap/ingestion_http.py` | HEAD probe, per-host throttle, download stats. |
| `djangoscrap/ingestion_adapters/` *(planned)* | Per-site extractors (Instagram account archiver, Pinterest scroller, etc.). |
| `djangoscrap/management/commands/` | Batch tooling: `purge_ingestion_tombstones`, `dedupe_ingestion_batches`, `backfill_clip_embeddings`, `run_ingestion_campaigns`. |
| `IngestionBatch` / `IngestionItem` models | Per-batch state, per-item provenance, `clip_embedding` field (512-D). |

### Campaign state tracked per batch

- `campaign_enabled`, `campaign_cadence_minutes`, `campaign_next_run_at` — scheduling
- `campaign_target_count` — folder size goal
- `campaign_max_pages_per_run`, `campaign_max_candidates_per_run` — work cap
- `campaign_state` (JSON) — vision-QC prefs, last-run variety seed, last-sync markers
- `campaign_last_report` — single-line diagnostics including:
  - `url-rewriter: in=N upgraded=N dropped=N rules=pinterest:N,…`
  - `download: probed=N head_blocked=N get_ok=N reasons=too_small:N,http_404:N,…`

---

## Status — what's shipped vs planned

### ✅ Shipped

1. **Three-layer semantic dedupe** — SHA256, pHash, and CLIP ViT-B/32 cosine
   similarity (threshold 0.92). Blocks "same scene, different encoding" dupes
   that phash alone misses.
2. **Thumbnail → full-resolution URL rewriter** — 11 site rules upgrading
   Pinterest `/236x/` → `/originals/`, Etsy `il_570xN` → `il_fullxfull`,
   Flickr size-suffix upgrades, Tumblr, Jetpack photon, 500px, Unsplash,
   Wikimedia thumb-strip, Reddit preview rehome.
3. **Watermark-host ignore list** — Shutterstock/Dreamstime/Freepik/Vecteezy/
   PngTree/Adobe-Stock/Getty/iStock preview CDNs are dropped pre-download.
4. **JSON rule overlay** — drop `ingestion_site_rules.json` at the project
   root (or set `INGESTION_SITE_RULES=/path/to/…`) to add or override rules
   without editing Python. Schema in `ingestion_site_rules.example.json`.
5. **HEAD-probe before download** — 15 KB image minimum, 100 KB video minimum,
   wrong-content-type rejection, 404/410 pre-filter. Typical cycle blocks
   15-25% of candidates *before* the GET.
6. **Per-host throttle + politeness** — per-host semaphore + min-delay gate.
   Defaults 2 concurrent / 400 ms; tighter for `i.pinimg.com` (1 / 700 ms)
   and `i.redd.it` / `preview.redd.it` (1 / 1000 ms).
7. **Vision-QC auto-accept** — GPT-4o-mini judges candidate images against
   the concept brief and reference seeds; rejections delete the file and
   keep the row as dedupe signal only (no more dangling tombstones).
8. **Disk-cached composition previews** — HTTP ETag + on-disk JPEG cache,
   so the admin grid loads in ~1 request instead of N.
9. **CBIR (reverse-image search) via Yandex** — an accepted image becomes a
   next-cycle seed, so campaigns converge on an aesthetic.
10. **Campaign reporting** — every cycle writes a single-line diagnostic
    summary to `campaign_last_report` with rewriter + download stats
    visible in the UI.

### 🚧 Planned — cutting-edge roadmap

These items come from studying what the best-in-class bulk/social
downloaders do that we don't yet. See the **References** section below
for the specific projects each idea is adapted from.

| # | Work | Why it matters | Effort |
| --- | --- | --- | --- |
| 1 | Port [**maxurl**](https://github.com/qsniyg/maxurl) rule database into our JSON overlay | ~1000 site rules vs our 11. Upgrade rate jumps from ~10% to ~50% of candidates. | 1 day |
| 2 | **Playwright scroll-harvester** for Pinterest boards + Tumblr tags | Unlocks JS-heavy galleries Yandex indexes poorly; a single Pinterest board yields 50-100 fresh images at source resolution. | 1-2 days |
| 3 | **instagrapi replacement / fallback for instaloader** | Private-API-based, much faster, handles IG anti-scraping far better. Source: [subzeroid/instagrapi](https://github.com/subzeroid/instagrapi) 6.1k⭐. | 1 day |
| 4 | **yt-dlp for the 1800+ sites long tail** | Already imported for YouTube/TikTok. Expand to Reddit video, Bilibili, Douyin, Kuaishou, Pixiv, DeviantArt, Xiaohongshu. Source: [omniget](https://github.com/tonhowtf/omniget) architecture. | 0.5 day |
| 5 | **Account archiver mode** (new `IngestionBatch.source_kind="account"`) | One-click "archive @handle from site X." Per-account folder with resumable incremental sync via `last_post_id` marker — mirrors [Turbo Downloader](https://chromewebstore.google.com/detail/cpgaheeihidjmolbakklolchdplenjai). | 1 day |
| 6 | **Multi-engine search fan-out** | Parallel queries to Bing, DuckDuckGo, Pexels, Pixabay, Flickr, Unsplash, booru family (Safebooru/Gelbooru/Danbooru) alongside Yandex. Source: [CharlesPikachu/imagedl](https://github.com/CharlesPikachu/imagedl). | 1-2 days |
| 7 | **Multi-engine reverse-image search** (CBIR fan-out) | Yandex-CBIR + Google Lens + Bing Visual + TinEye in parallel on an example seed. Dramatically expands "find more like this." | 1-2 days |
| 8 | **Per-site DOM-aware extractors** (Pinterest, Pixiv, DeviantArt, Twitter/X, VK) | Builds on #2. Gets original-resolution assets from each site's quirky structure. Source: [KellyC-Image-Downloader](https://github.com/NC22/KellyC-Image-Downloader). | 2-3 days |
| 9 | **Concurrent URL scraping** inside a single page batch | Currently sequential. `asyncio.Semaphore`-gated concurrent HEAD+GET would 3-5x cycle throughput. | 0.5 day |
| 10 | **Concept-to-campaign auto-config** | "80s cyberpunk neon" → GPT picks engines, generates query pack, selects CBIR seeds from web, sets vision-QC prefs. Just-say-what-you-want mode. | 1-2 days |

---

## Social-media & account archive strategy

Modelled after the best patterns in the [instagram-downloader](https://github.com/topics/instagram-downloader)
ecosystem — specifically **instagrapi's** private-API approach combined with
**Turbo Downloader's** per-account folder convention.

### Adapter priority order

```text
Given a URL like https://instagram.com/<handle>  or  https://tiktok.com/@<handle>

  1. instagrapi     — if IG, use private API (fastest + most coverage: Posts,
                      Reels, Stories, Highlights, Tagged, Saved)
  2. yt-dlp         — ~1800-site registry: TikTok, X/Twitter, Reddit, Pixiv,
                      DeviantArt, Bilibili, Douyin, Kuaishou, Xiaohongshu, …
  3. gallery-dl     — long-tail fallback with its own ~400 extractors
  4. Playwright     — JS-heavy sites that none of the above handle
  5. Raw HTML       — last-resort extractor from rendered page
```text

### Account archive contract

- Folder convention: `<site>_<handle>__<subject-if-known>__<site>`
  (e.g. `instagram_liamwong__neon__instagram`)
- State tracked on `IngestionBatch.campaign_state.adapter_state`:
  - `last_post_id` / `last_post_ts` — highwater marker
  - `carousel_depth_completed` — so interrupted archives resume mid-post
- On re-run the adapter fetches only posts newer than `last_post_id`.
- Every post's media (carousel items, Reels, Stories, Highlights where
  applicable) flows through the normal HEAD/throttle/dedupe/QC pipeline.

---

## Aesthetic continuity

The CLIP embedding layer is what makes this a *composition* tool rather
than a stock downloader. Every accepted image gets a 512-D embedding stored
inline on its row. That enables:

1. **Semantic dedupe** — "twenty different blue-fur shots from slightly
   different angles" collapse into one representative.
2. **Similarity-guided sourcing** — an accepted image is a CBIR seed for
   the next cycle, so the batch converges on a visual direction.
3. **Cross-batch recommendations** (future) — "find other folders with
   aesthetic overlap" via cosine similarity across batch centroids.

The CLIP model is loaded lazily on first use; if `torch` / `sentence-transformers`
aren't importable the pipeline falls back to SHA256 + pHash only without
breaking ingestion. This keeps the optional dependency genuinely optional.

---

## Operational commands

```bash
# Run a single campaign cycle on one batch (diagnostic / manual kick).
python manage.py shell
  >>> from djangoscrap.models import IngestionBatch
  >>> from djangoscrap.views import _run_campaign_cycle
  >>> _run_campaign_cycle(IngestionBatch.objects.get(id=26), trigger="manual")

# Run scheduled cycles for every campaign-enabled batch (cron job).
python manage.py run_ingestion_campaigns

# Remove rows with missing files (tombstones) on one batch.
python manage.py purge_ingestion_tombstones --batch 26

# Re-dedupe a batch after adjusting thresholds.
python manage.py dedupe_ingestion_batches --batch 26 --mode collapse

# Backfill CLIP embeddings on historical items (first-run cost: ~600 MB
# model download from Hugging Face).
python manage.py backfill_clip_embeddings --batch 26

# Reload JSON rule overlay at runtime without restart.
python manage.py shell
  >>> from djangoscrap.ingestion_url_rewriter import reload_rules
  >>> reload_rules()
```text

---

## Reading a `campaign_last_report`

Every cycle produces a single-line diagnostic. Example:

```text
trigger=manual_verify; pages=4 (similar=3, text=1); cbir_seeds=5;
candidates=50; imported=22; duplicates=15; filtered=0; failed=13;
url_skipped=0; qc_checked=31; qc_rejected=9; folder=560/800; queue=101;
engines=playwright-html:3,yandex-html:1;
url-rewriter: in=69 upgraded=7 dropped=0 rules=pinterest:7;
download: probed=50 head_blocked=11 head_unsup=2 get_ok=37 get_failed=2
  reasons=too_small:7,head_forbidden:2,get_http_403:2,http_400:2,http_404:1;
review_then_publish
```

Field cheatsheet:

- **candidates** — distinct URLs considered this cycle (post-rewrite, pre-dedupe).
- **imported** — new items added to the batch.
- **duplicates** — caught by SHA256/pHash/CLIP layer.
- **filtered** — rejected by size/orientation/color prefs.
- **qc_checked** / **qc_rejected** — vision-QC judged off-topic.
- **url-rewriter: upgraded=N** — thumbnails upgraded to full-res.
- **url-rewriter: dropped=N** — URLs on watermark-host ignore list.
- **download: head_blocked=N** — rejected before download (saved bandwidth).
- **download: reasons=** — histogram of block causes: `too_small`, `wrong_type`,
  `http_404`, `http_400`, `head_forbidden`, `head_timeout`, `missing_content_type`.

Look for:

- `upgraded` > 0 → rewriter is pulling its weight for this batch's sources.
- `head_blocked / probed` ≈ 20-30% → pipeline is protecting the grid from junk.
- `duplicates / candidates` high but `imported` still > 0 → CBIR is drawing from
  the right neighborhood but too similar; raise CLIP cosine threshold.

---

## References (what we're learning from)

Our roadmap items are adapted from these projects. Named credit where adopted:

- **[Turbo Downloader for Instagram](https://chromewebstore.google.com/detail/cpgaheeihidjmolbakklolchdplenjai)** — per-account folder + incremental resume + stream-to-disk pattern.
- **[instaloader](https://github.com/instaloader/instaloader)** (12.2k⭐) — current IG extractor; baseline.
- **[subzeroid/instagrapi](https://github.com/subzeroid/instagrapi)** (6.1k⭐) — private-API replacement; the modern leader for IG.
- **[tonhowtf/omniget](https://github.com/tonhowtf/omniget)** (1.8k⭐) — yt-dlp as a universal 1800-site extractor is the right architectural layer.
- **[qsniyg/maxurl](https://github.com/qsniyg/maxurl)** (1.5k⭐) — ~1000-site URL upgrade rule database. The rulebook we want to port into our JSON overlay.
- **[CharlesPikachu/imagedl](https://github.com/CharlesPikachu/imagedl)** (111⭐) — multi-engine fan-out across 20+ search engines + booru family.
- **[NC22/KellyC-Image-Downloader](https://github.com/NC22/KellyC-Image-Downloader)** (304⭐) — per-site DOM-aware extractors for Twitter/VK/Pinterest/Pixiv/DeviantArt.
- **[hoothin/UserScripts — DownloadAllContent](https://github.com/hoothin/UserScripts)** (4.1k⭐) — the infinite-scroll harvest pattern we need for Pinterest/Tumblr.
- **[ultralytics/flickr_scraper](https://github.com/ultralytics/flickr_scraper)** (279⭐) — Flickr merits a dedicated adapter due to tight rate limits.
- **[maja-829/bulk-image-downloader](https://github.com/maja-829/bulk-image-downloader)** — concurrent multi-URL + proxy pattern.

---

## Design principles (the non-negotiables)

1. **Optional dependencies stay optional.** CLIP, Playwright, instagrapi all
   fail-soft: the pipeline falls back to simpler layers if an import fails.
2. **Observability in one line.** Every cycle's `campaign_last_report`
   tells you what happened and why, no log spelunking needed.
3. **Idempotent re-runs.** Running the same campaign twice costs almost
   nothing — the three-layer dedupe catches everything. Re-running a
   failed batch is safe.
4. **Polite by default, not correct-at-all-costs.** HEAD probe, throttle,
   and rule-based ignores prevent us from becoming a well-behaved CDN's
   enemy. Better to source 80 good images from 10 sites than 100 and get
   rate-limited next week.
5. **Hidden complexity.** The user should never need to know about CLIP,
   pHash, HEAD probes, or per-host semaphores. They describe a folder; it
   fills and stays fresh.
