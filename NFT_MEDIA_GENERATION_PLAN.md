# NFT Media Generation Plan

This document is the operational reference for how `The Feed` produces the
artwork that backs each token: the square poster, the 10-second preview, the
45-second collector video, and the `tokenURI` metadata payload that ties them
together. It reflects the current code in
[`djangoscrap/nft_media.py`](djangoscrap/nft_media.py) and the related Django
views, models, and management command.

For the launch context (contract, mint UI, deploy plan) see
[`NFT_LAUNCH_README.md`](NFT_LAUNCH_README.md).

---

## 1. Goals & constraints

1. **Faithful capture**: media must look identical to the live `/?render=1`
   page so collectors get exactly what they previewed.
2. **Cheap public previews**: `/mint/` and OpenSea load a `preview_15s` file
   that should sit comfortably under 1 MB so the grid of 1000 tiles is fast
   on mobile and on cold S3/R2 caches.
3. **Premium collector copy**: the `collector_45s` file is the high-bitrate
   square master that owners can download from the detail page.
4. **Deterministic re-renders**: when source folders, settings, or audio
   change, regeneration is automatic and idempotent. When nothing changed,
   the pipeline is a no-op.
5. **Per-token versioning**: every successful regeneration is preserved as a
   new `CompositionNFT` row so old versions remain downloadable, and the
   `Updated` label on the detail page always reflects truth.
6. **Storage-portable**: outputs live under one prefix
   (`nft/generated/composition_<id>/...`) so they can be mirrored to R2,
   IPFS, and Arweave with a single rsync-style copy.

---

## 2. Asset matrix

Defined in `MEDIA_KINDS` (`djangoscrap/nft_media.py`):

| Kind | Use | Dimensions | Duration | FPS | Codec | CRF | Audio | Target size |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `poster` | Square still / OpenSea fallback | 1080×1080 | – | – | JPEG q=92 | – | – | < 200 KB |
| `preview_15s` | `/mint/` grid + OpenSea `animation_url` | 720×720 | 10 s | 24 | H.264 (yuv420p, faststart) | 32 | AAC 64 kbit | < 1 MB |
| `collector_45s` | Owner / collector download | 1080×1080 | 45 s | 25 | H.264 (yuv420p, faststart) | 23 | AAC 128 kbit | 8-15 MB |

The kind name `preview_15s` is historical — the actual duration is now 10 s
because OpenSea hover previews stop after the first ~10 s and shorter clips
trim more weight than they cost in legibility. Renames are deferred to avoid
churning past `source_signature` cache hits.

---

## 3. Capture pipeline

```
Composition → /<slug>/?render=1 → Playwright → raw .webm → ffmpeg → final mp4 → default_storage
```

### 3.1 Playwright

Implementation: `_launch_chromium`, `_wait_for_render_ready`,
`capture_composition_still`, `capture_composition_video`.

- Launches headless Chromium with autoplay + animation flags so the rendered
  composition runs without user gesture.
- Sets the viewport to the target square dimensions (1080×1080 for collector
  and poster, 720×720 for preview).
- Navigates to `<composition.url>?render=1`. The `?render=1` flag tells the
  page to expose `window.__compositionCaptureReady = true` once the first
  full frame is composited.
- Waits up to 120 s for that flag, then idles 1.2 s of warmup so transitions
  settle before the first recorded frame.
- For videos: uses Playwright's `record_video_dir` mode to capture a raw
  WebM at the viewport size.

### 3.2 ffmpeg

After Playwright finishes, the raw capture is re-encoded:

```bash
ffmpeg -y -i <capture.webm> \
       [-stream_loop -1 -i <audio_file>] \
       -map 0:v:0 -c:v libx264 -pix_fmt yuv420p -r <fps> \
       -preset medium -crf <crf> -movflags +faststart \
       [-maxrate 700k -bufsize 1400k]                  # preview only
       [-map 1:a:0 -c:a aac -b:a <audio_bitrate>]      # if audio
       -ss 0.7 -t <duration> <output.mp4>
```

- The first 0.7 s is trimmed to skip the cold-start jitter from the browser
  context.
- Audio is looped (`-stream_loop -1`) so that even short audio assets cover
  the full collector duration without silence at the tail.
- `-movflags +faststart` keeps the moov atom at the beginning so OpenSea's
  HTTP range-requested preview starts playing immediately.
- Preview bitrate is capped via `-maxrate 700k -bufsize 1400k` to keep the
  file size under 1 MB even for high-motion compositions; the collector
  copy uses CRF 23 with no cap so visuals stay clean.

### 3.3 Poster

Same Playwright launch, but a single `page.screenshot(type="jpeg", quality=92)`
at the viewport size. No ffmpeg pass.

---

## 4. Source-signature fingerprint

`composition_source_signature(composition)` returns a `sha256` over a JSON
payload that contains:

- composition primitives (`type`, `transition`, `playback_speed`,
  `landscape_only`, brightness/saturation/opacity, filter preset + params,
  `filter_settings`, all overlay flags)
- the audio file's storage state (name, size, mtime)
- the **full file inventory** of every referenced source folder under
  `composition_sources_unprocessed/<name>/` (file count, byte total, latest
  mtime, first 80 sample paths)

If any byte in any input changes, the signature changes. The pipeline stores
this hash on `CompositionMediaAsset.source_signature` and short-circuits
regeneration whenever:

```python
asset.status == "ready"
asset.file is present in default_storage
asset.source_signature == composition_source_signature(composition)
```

Forced regeneration (`generate_nft_media --force`) bypasses the check and
always re-renders.

---

## 5. Storage layout

```
default_storage://nft/generated/composition_<id>/
├─ poster.jpg
├─ preview_15s.mp4
├─ collector_45s.mp4
└─ metadata.json   # written separately when a CompositionNFT version is prepared
```

Old `CompositionNFT` rows (per-version snapshots) keep their own paths via
`local_collector_video_file`, `local_video_file`, `local_image_file`, and
`local_metadata_file`. These are append-only — newer versions never overwrite
older ones, which is what powers the "Old versions" download list on the
single-composition mint page.

---

## 6. Versioning model

Two related Django models drive everything:

| Model | Role |
| --- | --- |
| `CompositionMediaAsset` | The **current** rendered file per `(composition, kind)`. Replaced in place when `source_signature` changes. Source of truth for `/mint/` previews and metadata. |
| `CompositionNFT` | An **immutable snapshot** taken at the moment a version was prepared for minting. Carries its own copies of the image / video / collector video / metadata, plus chain context (`token_id`, `tx_hash`, `owner_wallet`). |

The detail page surfaces:

- The current `CompositionMediaAsset.collector_45s` (download + size + last
  generated_at) at the top of the Media accordion.
- A list of every prior `CompositionNFT` version with `local_collector_video_file`
  set, ordered newest-first, with download URL + size + `updated_at_label`.

This means there is one canonical "preview" per composition (always fresh)
and a permanent audit trail of every collector cut that was ever published.

---

## 7. Generation runbook

### 7.1 Management command

```bash
python manage.py generate_nft_media \
    [--composition-id <id> ...] \
    [--ready-only] \
    [--kinds poster,preview_15s,collector_45s] \
    [--force]
```

Defaults:

- All compositions with a non-empty `url`, ordered by `id`.
- All three kinds (`poster`, `preview_15s`, `collector_45s`).
- Skips assets whose `source_signature` already matches and whose stored
  file still exists.

Common operations:

- Regenerate everything for the launch: `--force`.
- Refresh a single work after editing it: `--composition-id 42`.
- Production sweep that only touches deployable works:
  `--ready-only --force`.

### 7.2 Per-asset state machine

```
pending → rendering → ready
                 ↓
              failed (with error_message)
```

`status="rendering"` is set right before the Playwright launch and surfaces
in the admin so an operator can see in-flight jobs. On failure the file is
left untouched, `error_message` is truncated to 2 KB, and the next run
retries automatically.

### 7.3 Concurrency

The current command runs serially. Each asset takes roughly:

| Kind | Wall-clock per composition |
| --- | --- |
| `poster` | 6-10 s |
| `preview_15s` | 15-25 s |
| `collector_45s` | 50-90 s |

Full-collection rebuild today: ~1 hour for ~16 compositions. At 1000 supply
the underlying composition count stays small (the random allocator maps 1000
tokens onto a handful of works), so this remains tractable. If concurrency
becomes a bottleneck, the natural move is to wrap
`generate_composition_media_assets` as a Celery task and run a worker pool
sized to one Chromium instance per CPU core (see §11).

---

## 8. Quality and size budget

| Constraint | Budget | Why |
| --- | --- | --- |
| `preview_15s` file size | ≤ 1.0 MB | OpenSea cold-cache hover, mobile data on `/mint/` grid |
| `preview_15s` perceived quality | "watchable, not pristine" | CRF 32 + 700 kbit cap. Acceptable trade for size. |
| `collector_45s` file size | ≤ 20 MB hard cap | OpenSea will refuse very large `animation_url` blobs; the collector file is downloaded directly from the contract page and is allowed to be larger but should still stay sane. |
| `collector_45s` perceived quality | "transparent" | CRF 23, no maxrate cap. |
| Poster | ≤ 200 KB JPEG q=92 | Acts as the thumbnail OpenSea falls back to when `animation_url` fails to play. |

If a future composition consistently busts the preview budget, options are
(in order): drop FPS to 20, drop resolution to 640×640, raise CRF to 34.
Avoid lowering the duration further — under 8 s reads as a glitch.

---

## 9. `tokenURI` metadata schema

Built by `_build_nft_metadata_payload(request, composition)` and persisted
to `nft/generated/composition_<id>/metadata.json` whenever a
`CompositionNFT` version is prepared.

```json
{
  "name":          "<composition.nft_name | composition.name>",
  "description":   "<composition.nft_description | autogen fallback>",
  "image":         "https://.../nft/generated/composition_<id>/poster.jpg",
  "animation_url": "https://.../nft/generated/composition_<id>/preview_15s.mp4",
  "external_url":  "<composition.nft_external_url | page_url | url>",
  "attributes": [
    {"trait_type": "Composition ID",        "value": <id>},
    {"trait_type": "Mode",                  "value": "live" | "frozen"},
    {"trait_type": "Type",                  "value": "<classic|...>"},
    {"trait_type": "Mood",                  "value": "<low|mid|high>"},
    {"trait_type": "Duration Seconds",      "display_type": "number", "value": 10},
    {"trait_type": "Collector Video Seconds","display_type": "number", "value": 45},
    {"trait_type": "Playback Speed",        "display_type": "number", "value": 1.0},
    {"trait_type": "Source Playback",       "value": "<chronological|random|...>"},
    {"trait_type": "Brightness",            "display_type": "number", "value": 50},
    {"trait_type": "Saturation",            "display_type": "number", "value": 50},
    {"trait_type": "Opacity",               "display_type": "number", "value": 100},
    {"trait_type": "Themes",                "value": "tag1, tag2, ..."}
  ],
  "properties": {
    "composition_id":          <id>,
    "composition_url":         "<live url>",
    "page_url":                "<public page>",
    "metadata_generated_at":   "<iso-8601>",
    "nft_enabled":             true,
    "mode":                    "live",
    "collector_video_url":     "https://.../collector_45s.mp4"
  }
}
```

### 9.1 `mode` semantics

- `mode = "live"` → the `image` and `animation_url` are URLs that will keep
  receiving fresh re-renders whenever the composition's source pool evolves.
  This must be disclosed in `description` so collectors understand the work
  is intentionally non-static.
- `mode = "frozen"` → the URLs reference an IPFS-pinned file from the
  `CompositionNFT` snapshot at mint time and never change.

For `The Feed` launch the default is `live` (matches the project intent),
but the IPFS-pinned collector cut is always preserved per-version so the
provenance log stays intact even if the live URL drifts later.

### 9.2 Provenance hash

Before opening the sale, run `python manage.py compute_metadata_provenance`
(to be added — see §11) which:

1. Iterates the planned 1000-token mapping in the agreed order.
2. Streams each metadata file's bytes into a rolling `keccak256`.
3. Emits the final 32-byte hash.

That hash is the value passed to `setProvenance(...)` on the contract.

---

## 10. Stale detection

The detail-page banner that previously read **"Generated media is stale"**
has been hidden from public view (`/mint/<slug>/` no longer renders it) but
the data is still computed and exposed in admin via
`CompositionMediaAsset.is_stale`:

```python
@property
def is_stale(self):
    return bool(self.source_signature) and \
           self.source_signature != composition_source_signature(self.composition)
```

Operational rule: the public mint page must never display a stale asset.
Cron a `generate_nft_media --ready-only` sweep nightly so any drift caught
by `is_stale` is corrected before a collector sees it.

---

## 11. Failure modes and mitigations

| Failure | Symptom | Mitigation |
| --- | --- | --- |
| Playwright render timeout | `composition_capture_ready` never fires within 120 s | Asset persists as `failed`, `error_message` stored. Operator opens the page manually with `?render=1` to debug client-side issue, then reruns the command. |
| ffmpeg encode failure | Non-zero exit, stderr captured | Captured as `error_message`. Re-run normally; if persistent, raise CRF or drop FPS until the encoder accepts the stream. |
| Audio file missing | Composition references a file that's been deleted | Pipeline silently skips the audio mux (no crash) and produces a video-only mp4. The detail page surfaces this via the asset's `is_stale` property. |
| Storage write race | Two workers regenerate the same asset | The `_store_file` step does an atomic `delete + save` pair; the loser overwrites the winner with identical bytes (signature is deterministic). No data loss. |
| OpenSea rejects `animation_url` | File too large, wrong codec | Preview budget keeps us well under thresholds. Hard ceiling: ~20 MB and `video/mp4` H.264; we ship 0.5-1 MB H.264. |
| Live volume macOS resource forks (`._*`) on the dev machine | None at runtime; only breaks Foundry build | Documented in `contracts/Makefile`'s `dotclean` target. |

---

## 12. Roadmap

Tracked separately from launch-blocking work. None of these are required for
mainnet day-1.

1. **Celery worker integration**
   - Wrap `generate_composition_media_assets` as a task.
   - One worker per CPU core, each with its own Chromium pool.
   - Per-task timeout of 180 s, exponential backoff on failure.
2. **Mezzanine master**
   - Optional ProRes 4444 master alongside the H.264 collector for archival
     so we can re-encode for codecs we don't yet support (AV1, HEVC).
3. **Provenance command**
   - `python manage.py compute_metadata_provenance` to emit the keccak256
     value used in `setProvenance(...)`.
4. **IPFS auto-pin**
   - Pin newly-rendered files to two providers (Pinata + nft.storage) on
     successful regeneration.
   - Persist the resulting CIDs on `CompositionMediaAsset` so `tokenURI`
     can switch from `https://...` to `ipfs://...` without a re-render.
5. **Per-version preview**
   - Currently the public `/mint/` grid only references the **current**
     `preview_15s`. Add an opt-in "history" view for collectors that lists
     every preceding cut.
6. **Render farm parallelism**
   - When the catalogue grows past ~50 active compositions a single worker
     can no longer rebuild in under an hour. Plan for a horizontal worker
     pool sharded by `composition_id`.
7. **Watermarked teaser**
   - Optional 5 s, 480×480, lossy-JPEG-frames preview for embedding on
     social previews where even 1 MB is too much.

---

## 13. Quick reference

| Need | Command / location |
| --- | --- |
| Regenerate one composition | `python manage.py generate_nft_media --composition-id <id> --force` |
| Regenerate everything launch-ready | `python manage.py generate_nft_media --ready-only --force` |
| Inspect current asset state | Django admin → `Composition media assets` |
| Inspect saved version snapshots | Django admin → `Composition NFTs` |
| Source-signature implementation | `djangoscrap/nft_media.py::composition_source_signature` |
| Asset specs | `djangoscrap/nft_media.py::MEDIA_KINDS` |
| Metadata payload builder | `djangoscrap/views.py::_build_nft_metadata_payload` |
| Public detail UI | `djangoscrap/templates/mint/composition.html` |
| Public grid UI | `djangoscrap/templates/mint/site.html` |
