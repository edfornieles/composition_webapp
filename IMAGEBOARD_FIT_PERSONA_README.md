# /fit/ Corpus-Grounded Persona

A pipeline that turns the 4chan /fit/ board into a corpus-grounded inner voice for
Ed Fornieles' aggregation works. The persona speaks **from the corpus**, not from
training-time priors. If the corpus cannot supply enough grounded fragments for a
given tick, the persona returns nothing — there is no generic AI fallback.

This is **not** a generic AI roleplay character. The point is to expose what the
discipline-and-shame tone of /fit/ actually is, in its own language, alongside the
artworks.

## Architecture

```
djangoscrap/imageboard_ingestion/
  fourchan_client.py     rate-limited 4chan JSON API (1 req/s, User-Agent set)
  eightkun_client.py     stub, disabled by default
  adapters.py            raw post → manifest record (text + tags + safety)
  normalise.py           HTML → plain_text / quote_removed_text + reply links
  safety.py              regex classifier (doxxing, hate, violence, CP, sourcing, ED)
  storage.py             JSONL manifest writer, on-disk corpus layout
  profile_builder.py     builds fit_profile.json by counting drives/fears/objects
  embeddings.py          stdlib TF-IDF retrieval index
  thought_grounding.py   generate_fit_grounded_thought (≥3 fragments required)
```

## On-disk corpus layout

```
corpora/imageboards/4chan__fit__body_discipline/
  raw_json/fit/<thread_no>.json
  text/fit/<thread_no>/<post_no>.txt
  images/fit/<tim><ext>          (only with --download-images)
  thumbnails/fit/<tim>s.jpg
  manifests/4chan__fit__body_discipline.jsonl
  profiles/fit_profile.json
  embeddings/fit_tfidf.pkl
```

The whole `corpora/imageboards/` tree is gitignored.

## Manifest record schema (one line per post)

```jsonc
{
  "post_uid": "4chan__fit__body_discipline:fit:1234567",
  "source_key": "4chan__fit__body_discipline",
  "source": "4chan",
  "board": "fit",
  "thread_no": 90000001,
  "post_no": 1234567,
  "is_op": true,
  "subject": "/fraud/",
  "name": "Anonymous",
  "trip": "",
  "time_unix": 1730000000,
  "scraped_at_unix": 1730500000,
  "reply_targets": [1234500],
  "raw_html": "...",
  "plain_text": "...",
  "quote_removed_text": "...",
  "model_safe_text": "...",        // "" when high-risk labels fired
  "fit_tags": ["discipline_routine", "self_loathing"],
  "safety": {
    "likely_doxxing": false,
    "likely_hate": false,
    "likely_violence_incite": false,
    "likely_cp_indicator": false,
    "likely_steroid_sourcing": false,
    "likely_eating_disorder": false,
    "contains_external_link": false
  },
  "image": { "tim": 1730000000000, "ext": ".jpg", "filename": "...",
             "w": 1024, "h": 768, "fsize": 123456, "md5": "..." }
}
```

## Safety policy

`safety.classify(text)` runs a conservative regex sweep. If **any** of the
high-risk labels fire (doxxing, hate slurs, violence incitement, CP indicator,
steroid sourcing, eating-disorder glorification), `model_safe_text` is set to
`""`. The profile builder, the TF-IDF index, and the grounding layer **only ever
see `model_safe_text`**, so a high-risk post is structurally incapable of
flowing into the model-facing voice.

URLs are stripped from `model_safe_text` regardless.

The list is intentionally conservative — false positives are cheap, false
negatives are expensive. This is a safety filter, not a moderation system.

## Grounding contract

`generate_fit_grounded_thought()`:

1. Loads the persisted TF-IDF index.
2. Builds a query from the active scenario (composition title, hashtags, tile
   label) plus a sample of the profile's top drives/fears/objects.
3. Retrieves the top-k matching corpus fragments.
4. **Requires at least `MIN_FRAGMENTS = 3` fragments.** If retrieval returns
   fewer, the function returns `(None, debug)` and the wall runtime skips the
   tick. There is no roleplay fallback.
5. Composes the thought directly from quoted fragments.

A model-conditioned rephrase variant is intentionally not implemented in the
first version; the corpus is the voice.

## Wiring into the existing wall

`monologue_streaming.generate_stream_segment(persona, ...)` checks
`persona.slug` against `settings.FIT_PERSONA_SLUGS`. If it matches, the call is
routed through the grounded pipeline. If grounding fails and
`FIT_THOUGHTS_REQUIRE_GROUNDING` is `True` (default), an empty string + error
reason are returned — the wall runtime treats this as a no-op tick.

To activate the persona on a wall:

1. Create a `MonologuePersona` row with `slug="fit"` (or any slug listed in
   `FIT_PERSONA_SLUGS`).
2. Add `persona:<id>` to the run's `active_character_keys`.
3. Run the scrape + profile + embeddings commands below.

## Settings

```python
FIT_THOUGHTS_REQUIRE_GROUNDING = True         # default
FIT_CORPUS_SOURCE_KEY = "4chan__fit__body_discipline"
FIT_PERSONA_SLUGS = ("fit", "fourchan_fit_body_discipline")
ENABLE_8KUN_INGESTION = False                 # 8kun adapter is a stub
EIGHTKUN_HOST = ""
```

## Management commands

```bash
# 1. Scrape /fit/ (idempotent, rate-limited)
python manage.py scrape_fit_board --max-threads 20 --max-posts-per-thread 200
python manage.py scrape_fit_board --download-images --thumbnails-only

# 2. Aggregate the corpus statistics into a profile JSON
python manage.py build_fit_profile

# 3. Build the TF-IDF retrieval index over model-safe posts
python manage.py build_fit_embeddings

# 4. Smoke-test the grounded generator
python manage.py test_fit_thought --debug

# 5. Wipe the corpus (dry by default; --confirm to actually delete)
python manage.py delete_fit_corpus --confirm
```

## Tests

```bash
python -m pytest djangoscrap/tests/test_imageboard_*.py
```

Tests cover: HTML normalisation and reply extraction; safety classification;
the 4chan client (mocked, no network); manifest building; profile aggregation;
the ≥3-fragment grounding contract.

## What this is not

- Not a moderation system. Don't use `safety.py` to make decisions about
  whether a post is "okay"; only to decide whether it can flow into the model-
  facing voice.
- Not a therapy tool. The persona is a discipline-and-shame inner voice
  intended to play next to artworks about that discipline-and-shame. It is not
  a coach.
- Not a complete imageboard archiver. The pipeline keeps the smallest amount
  of data needed to ground the voice.

## 8kun

The 8kun adapter is a stub. It will refuse to make requests until both
`ENABLE_8KUN_INGESTION` and `EIGHTKUN_HOST` are set, and the actual fetch
methods have been implemented and reviewed. Do not enable this without
revisiting the safety regexes for board-specific patterns.

## Using ImagePlot for visual corpus research

The text-grounded persona above ignores the *image* side of /fit/. For visual
research and clustering — surfacing the recurring compositional types of
selfies, progress pics, gym mirror shots, supplement bottles, greentext memes,
diet-comparison infographics — we export the corpus to a CSV that loads into
[Lev Manovich's ImagePlot 2.2](https://manovich.net/index.php/projects/imageplot)
or any browser-based image-collection visualiser (Bokeh, Plotly, ml4a's
image-tSNE viewer, etc.).

This is **for visual research and clustering, not for model training.** The
exporter applies hard-safety blockers (`likely_cp_indicator`, `likely_doxxing`,
`likely_hate`, `likely_violence_incite`, `likely_eating_disorder`) and drops
anything that trips them, but is otherwise liberal.

### Workflow

1. **Scrape /fit/** as usual (`scrape_fit_board`, `scrape_fit_archive`).
2. **Export an ImagePlot dataset:**

   ```bash
   python manage.py export_fit_imageplot_dataset \
     --corpus fourchan_fit \
     --corpus-root "/Volumes/oom/imageboard_corpora" \
     --output "/Volumes/oom/imageboard_corpora/fourchan_fit/imageplot_exports/fit_imageplot.csv" \
     --image-mode thumbnails
   ```

   One row per usable image. Columns include `local_image_path`,
   `local_thumbnail_path`, `width`/`height`/`aspect_ratio`, the seven
   contamination axes (`body_shame`, `discipline_fetish`, `status_anxiety`,
   `supplement_obsession`, `injury_fear`, `self_loathing`, `greentext_energy`),
   `fit_tags`, `text_excerpt_safe`, and a first-guess `suggested_cluster_label`.

3. **Build a portable sample** (recommended for ImagePlot, which expects all
   images in one folder next to the CSV):

   ```bash
   python manage.py build_fit_imageplot_sample \
     --corpus fourchan_fit \
     --sample-size 1000 \
     --image-mode thumbnails \
     --corpus-root "/Volumes/oom/imageboard_corpora"
   ```

   Writes:
   ```
   <corpus_root>/fourchan_fit/imageplot_exports/
     fit_imageplot_sample.csv
     images/                   ← symlinks into ../../thumbnails/fit/
   ```

   Symlinks are used by default. Pass `--copy-images` for tools that don't
   follow symlinks, or when you want a self-contained folder you can move to
   another machine.

4. **Open ImagePlot** (`ImagePlot.html` or the Java app) in a browser.
5. **Load** the CSV and the sibling `images/` directory. ImagePlot will arrange
   thumbnails on a 2D scatter plot and let you re-axis on any numeric column —
   try `aspect_ratio` against `contamination_body_shame`, or `width` against
   `contamination_status_anxiety`. For dimensionality reduction, run UMAP on
   the contamination columns (or on CLIP embeddings of the thumbnails) and add
   `umap_x` / `umap_y` columns yourself — ImagePlot will use them as axes.
6. **Cluster visually** by dragging a marquee around groups of thumbnails that
   read as the same compositional type. Inside ImagePlot, fill in the
   `cluster_label_manual` column for those rows. Optionally also fill
   `screen_assignment` (which gallery screen this cluster will run on) and
   `composition_bucket` (which composition source-bucket the cluster feeds).
7. **Save** the labelled CSV (e.g. `fit_imageplot_labelled.csv`) and feed it
   back into the corpus:

   ```bash
   python manage.py import_fit_imageplot_labels \
     --labels-csv "/Volumes/oom/imageboard_corpora/fourchan_fit/imageplot_exports/fit_imageplot_labelled.csv" \
     --corpus fourchan_fit \
     --corpus-root "/Volumes/oom/imageboard_corpora"
   ```

   This writes `<corpus>/manifests/imageplot_labels.jsonl`, one record per
   labelled image, joinable to the main manifest by `id` / `post_id` /
   `thread_id`. Downstream code uses this label file to:

   - feed visual clusters into **composition source buckets** (so a screen
     showing the "gym mirror selfie" composition draws only from posts in that
     visual cluster);
   - drive **screen assignments** in multi-screen installs;
   - bias **AI thought contexts** — when the persona ticks, the surrounding
     image set can be filtered to the cluster currently on-screen so the inner
     voice and the imagery rhyme.

### Granularity (the "sliders")

Three knobs control how granular the visual buckets get:

- **`--sample-size`** at sample-build time — smaller samples (200–500) give
  you a coarser, faster-to-cluster view; larger samples (5000+) reveal more
  micro-types but take longer to navigate.
- The **contamination-axis thresholds** inside ImagePlot — gating on e.g.
  `contamination_body_shame >= 0.7` narrows the field to a single emotional
  register before clustering.
- The **cluster count** you draw by hand inside ImagePlot — there is no
  automatic k. The exporter intentionally leaves clustering to the human eye;
  ImagePlot makes it cheap to relabel and re-export.

If you want sliders in a GUI rather than command-line flags, the same CSV
loads into `panel`/`bokeh` notebooks where range-sliders on each numeric
column give you live visual filtering before you commit to labels.

---

## Local large-corpus image intelligence pipeline

The 754-image ImagePlot exporter is the *research-table* layer. To scale to
50,000+ images on an external drive (e.g. `/Volumes/oom/chan_corpus`), the
pipeline below adds local-first ingestion, dedup, embeddings, captions,
clustering, dataset-browsing, and folder materialisation **without replacing
any of the existing ImagePlot commands** — every new layer is additive.

Roles:

- **ImagePlot** = visual research table / cultural analytics / human labels.
- **FiftyOne** = local browser-based dataset viewer.
- **fastdup** (optional) = dedupe + visual hygiene.
- **OpenCLIP** = emergent visual-semantic similarity.
- **BLIP / BLIP2** = image captions.
- **UMAP + HDBSCAN** = emergent categories (no hand-picked k).
- **Folder materialiser** = art-production output (one folder per cluster).

### 0. Storage setup

```bash
# point everything at the external drive
export CHAN_CORPUS_ROOT=/Volumes/oom/chan_corpus
```

You can also pass `--root /Volumes/oom/chan_corpus` to every command, or set
`CHAN_CORPUS_ROOT` in `settings.py`. The default falls back to that path.

The ingester creates this layout under `$CHAN_CORPUS_ROOT/`:

```
raw/<source>/<board>/threads/         JSON snapshots of catalog/threads
images/<source>/<board>/originals/    untouched original bytes
images/<source>/<board>/thumbnails/   ≤512 px JPEG previews (allow-listed only)
manifests/                             images.jsonl, posts.jsonl, captions.jsonl, ...
manifests/embeddings/                  openclip_*.npy + .ids.json
derived/                               cluster outputs, ImagePlot exports, folders/
quarantine/no_preview/                 illegal-or-borderline originals (NEVER thumbnailed)
```

Sensitive content is handled by a central `SafetyDecision` object. The
reason codes `suspected_underage_sexual_content` and
`illegal_sexual_content` cause **immediate deletion of bytes**; lesser
infringements go to `quarantine/no_preview/` and are never thumbnailed,
embedded, captioned, or surfaced in any preview UI.

### 1. Ingest a board

```bash
python manage.py ingest_imageboard_board \
    --source fourchan --board fit \
    --download-originals --download-thumbnails \
    --limit-threads 200
```

Resumable: re-running skips post UIDs already in `manifests/posts.jsonl`. Pass
`--force` to redo, `--dry-run` to see what would happen, `--no-originals` /
`--no-thumbnails` to control bytes on disk.

### 2. Quality + dedupe

```bash
python manage.py analyze_image_quality \
    --source fourchan --board fit
```

Computes Pillow features (phash, dhash, blur, brightness, saturation,
contrast, entropy) and groups exact (sha256) and near (phash hamming ≤ 4)
duplicates. Writes `image_features.parquet/.jsonl` and
`duplicates.parquet/.jsonl`. Pass `--use-fastdup` to additionally run
fastdup if installed.

### 3a. Embed (OpenCLIP)

```bash
python manage.py embed_imageboard_images \
    --source fourchan --board fit \
    --model ViT-B-32 --pretrained laion2b_s34b_b79k
```

Auto-selects MPS / CUDA / CPU. Resumes from any existing
`embeddings/openclip_<tag>_<source>_<board>.npy`.

### 3b. Caption (BLIP)

```bash
python manage.py caption_imageboard_images \
    --source fourchan --board fit \
    --model Salesforce/blip-image-captioning-base
```

Captions append to `manifests/captions.jsonl` keyed by `image_key`.

### 4. Cluster (UMAP + HDBSCAN)

```bash
python manage.py cluster_imageboard_images \
    --source fourchan --board fit \
    --method hdbscan --min-cluster-size 25
```

Writes `clusters.parquet` (with auto-suggested labels mined from captions +
fit_tags), `cluster_membership.parquet`, and `umap_projection.parquet`.

### 5. Enriched ImagePlot export

```bash
python manage.py export_imageplot_enriched_dataset \
    --source fourchan --board fit \
    --output /Volumes/oom/chan_corpus/derived/imageplot_exports/fit_enriched.csv \
    --sample-size 5000 \
    --materialise-symlinks
```

This is the original `export_fit_imageplot_dataset` CSV columns *plus* the
enrichment columns: `original_path`, `thumbnail_path`, `safe_for_preview`,
`sha256`, `phash`, `duplicate_group_id`, `near_duplicate_group_id`,
`blur_score`, `brightness`, `saturation`, `contrast`, `entropy`,
`openclip_umap_x/y`, `openclip_cluster_id`, `openclip_cluster_label_auto`,
`caption`, `caption_model`, `manual_cluster_label`, `aesthetic_bucket`,
`generated_folder_path`. The `--materialise-symlinks` flag drops a portable
`images/` directory next to the CSV.

### 6. FiftyOne browser

```bash
python manage.py open_imageboard_fiftyone \
    --source fourchan --board fit
```

Builds a local FiftyOne dataset with every enrichment field as a sample
attribute and launches the app. `--no-launch` to build without opening.

### 7. Materialise aesthetic / semantic folders

```bash
# automatic clusters, default symlinks
python manage.py materialise_imageboard_folders \
    --source fourchan --board fit \
    --by openclip_cluster_label_auto \
    --min-cluster-size 25 \
    --symlink

# filtered by emotional register
python manage.py materialise_imageboard_folders \
    --source fourchan --board fit \
    --by manual_cluster_label \
    --where "orientation=portrait,contamination_body_shame>=0.5,blur_score>=80"
```

Output: `$CHAN_CORPUS_ROOT/derived/folders/<source>_<board>/<by>/<label>/`,
one folder per group with `manifest.json`. Symlinks by default; pass
`--copy` to copy bytes.

### 8. Round-trip labels back

ImagePlot writes `manifests/imageplot_labels.jsonl` (one record per labelled
image, keyed by `id` / `image_key`). The enriched export and the folder
materialiser pick those up automatically — manual cluster labels, screen
assignments, and composition buckets become first-class facets across the
whole stack.

### Optional dependencies

| Layer        | Package(s)                          | Behaviour if missing                  |
|--------------|--------------------------------------|---------------------------------------|
| Pillow       | `Pillow`                             | required for ingest / quality        |
| imagehash    | `imagehash`                          | phash/dhash skipped                   |
| pyarrow      | `pyarrow`                            | Parquet skipped, JSONL still written |
| OpenCLIP     | `open_clip_torch`, `torch`           | Phase 3a unavailable                  |
| BLIP         | `transformers`, `torch`              | Phase 3b unavailable                  |
| UMAP         | `umap-learn`                         | falls back to PCA-2                   |
| HDBSCAN      | `hdbscan`                            | falls back to KMeans                  |
| fastdup      | `fastdup`                            | extra dedupe pass skipped             |
| FiftyOne     | `fiftyone`                           | Phase 6 unavailable                   |

Every command degrades gracefully and prints a clear "install X" hint
rather than crashing.
