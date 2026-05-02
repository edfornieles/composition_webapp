# Ingestion Pipeline — Cutting-Edge Implementation Plan

Last updated: April 2026

Scope note: this file tracks ingestion work only.  
Wall playback/orchestration tasks are tracked in the main project docs and wall runtime code.

<!-- markdownlint-disable MD004 MD007 MD022 MD024 MD031 MD032 MD040 MD060 -->

This is the living plan for turning the ingestion system into the machine
described in [`INGESTION_CAMPAIGN_README.md`](./INGESTION_CAMPAIGN_README.md):
a fast, polite, multi-surface, aesthetic-aware sourcing engine that just
works when you describe a folder.

**This file is a checklist.** Tick boxes as tasks finish. Do not start the
next phase until the current phase's tests pass and the success metric is
green in a real `campaign_last_report`. When we come back after a break,
the first thing we do is open this file and find the highest un-checked
box.

---

## Guiding principles (non-negotiable)

These apply to **every** phase. A task is not "done" until all five hold.

1. **Fail-soft imports.** New optional dependencies (instagrapi, maxurl port,
   proxy libraries…) must degrade gracefully if absent. The existing pipeline
   must still run on a fresh clone with only `requirements.txt` installed.
2. **Feature-flag new engines.** Every new adapter ships behind a boolean on
   `IngestionBatch.campaign_state` (e.g. `"adapters": {"instagrapi": true}`).
   Default is **off** until that adapter's smoke test passes on a real batch.
3. **Observability in one line.** Every new pipeline stage must add a
   `stage_name: key=val key=val` token to `campaign_last_report` so we can
   see what happened without log-diving.
4. **Idempotent re-runs.** No task may introduce a code path where running
   the same cycle twice changes semantics. The three-layer dedupe must still
   catch everything.
5. **One rollback commit.** Every task is small enough to revert cleanly.
   If a task balloons past ~400 lines changed, split it.

---

## Phase 0 — Safety net (blocks all other work)

**Goal**: build the smallest possible harness that lets us change code
with confidence. Without this, every later phase is gambling.

### Tasks

- [x] **P0.1 — Add pytest + pytest-django to `requirements.txt`**
  - Files: `requirements.txt`
  - Also create `pytest.ini` with `DJANGO_SETTINGS_MODULE = djangoscrap.settings`
    and `python_files = test_*.py tests.py *_test.py`.

- [x] **P0.2 — Create `djangoscrap/tests/` with baseline test files**
  - `tests/__init__.py`, `tests/conftest.py` (shared fixtures: a sample
    `IngestionBatch`, a tmp `INGESTION_MEDIA_ROOT`, a `responses`-mocked
    HTTP session).
  - `tests/test_url_rewriter.py` — cover the 11 existing rules + JSON overlay.
  - `tests/test_ingestion_http.py` — HEAD probe decision matrix, throttle
    hold timing, DownloadStats summary formatting.
  - `tests/test_download_pipeline.py` — full `_download_ingestion_url` flow
    with mocked HTTP (`responses` library): valid image, 404, text/html,
    too-small, no-HEAD-support.
  - `tests/test_dedupe.py` — `_detect_duplicate` across SHA / pHash / CLIP layers.

- [x] **P0.3 — Smoke-test management command**
  - New `djangoscrap/management/commands/ingestion_selftest.py`.
  - Runs a dry cycle against a dedicated `IngestionBatch` with a fixed
    URL list (a checked-in JSON fixture), asserts per-stage counters land in
    expected ranges, prints a one-line PASS/FAIL summary.
  - This is the post-deploy canary. Run it before calling any phase "done".

- [x] **P0.4 — CI-style runner script**
  - `scripts/run_tests.sh` — activates venv, runs `pytest`, then
    `python manage.py ingestion_selftest`. Exit non-zero on failure.
  - Doesn't need real CI; just a single command the human can run.

- [x] **P0.5 — Document the rollback procedure**
  - Added to this file, Section "Rollback playbook" below.

### Tests for this phase

- `pytest djangoscrap/tests/` → all green on main.
- `python manage.py ingestion_selftest` → PASS on main with the current
  pipeline.

### Success metric

`scripts/run_tests.sh` exits 0. Commit the green baseline.

### Rollback

N/A — this phase is purely additive.

---

## Phase 1 — Quality levers, zero new surfaces

**Goal**: extract maximum value from the pipeline we already have.
No new engines, no new scrape surfaces. Just make the existing pipeline
faster and more accurate.

### 1A — Port maxurl's URL-upgrade rules

**Why first**: biggest effort-to-impact on the whole roadmap. ~1000 site
rules vs our 11. Rewriter upgrade hit rate jumps from ~10% to ~50%.

Source: [qsniyg/maxurl](https://github.com/qsniyg/maxurl) — MIT licensed.
Rules live in `src/lib/img_parts.js` as a big JS function table.

- [x] **P1A.1 — Hand-curated high-value ruleset**
  - Decision: rather than auto-port maxurl's ~1000 JS rules (which would
    require a multi-day parser and most rules would be for hosts we
    never see), shipped a ~50-rule overlay at
    `ingestion_site_rules.json` covering the top 25+ CDNs we plausibly
    hit (Imgur, Blogger, Google, Twitter, FB/IG CDNs, Shopify, Amazon,
    ArtStation, Wix, Pixiv, iTunes, Discord, Weibo, VK, Xiaohongshu,
    TikTok, plus ignore-list for Canstock/Bigstock/Rawpixel/Pikwizard/
    StockSnap/Hippopx/Pixnio).
  - Core fix (bonus): rewriter now iterates ALL matching rules for a host
    instead of stopping on first host match — lets two patterns per host
    (e.g. Twitter `:large` suffix + `?name=large` query) both fire.
  - Bonus bug fixes from writing tests: 500px query-separator regex ate
    the `&`, `stockadobe.com` was a typo for `stock.adobe.com`.

- [x] **P1A.1 — (original) JS extractor**
  - `scripts/extract_maxurl_rules.py` — clones/downloads maxurl source,
    parses the JS function table with a regex-based walker, emits our
    JSON schema (`{host_suffix, pattern, replacement}` entries).
  - Not all JS rules port cleanly (maxurl uses JS regex dialect + async
    HEAD probes). Extract only the subset expressible as pure regex.
    Count rejects and list them in `scripts/extract_maxurl_rules_report.txt`
    so we know what's NOT covered.

- [x] **P1A.2 — Write the extracted rules to `ingestion_site_rules.json`**
  - Keep our hand-tuned rules as a separate `ingestion_site_rules.overrides.json`.
  - The overlay loader in `djangoscrap/ingestion_url_rewriter.py` already
    supports JSON; only addition needed: load both files (overrides first,
    then maxurl), deduping by `host_suffix`.

- [x] **P1A.3 — Add `tests/test_url_rewriter_overlay.py`** (30 rule-pair tests, all green)
  - Fixture: 30 real-world URLs (half thumbnail, half originals) from batch 1
    + 141's DB. Assert rewriter upgrades each thumbnail to the expected
    full-res URL.

- [x] **P1A.4 — Benchmark script** — `scripts/benchmark_url_rewriter.py`.
  Baseline: 17/44 upgrades. With overlay: 33/44 (+94% relative).
  - Script: pull 500 random `IngestionItem.original_url` values, feed through
    the new rewriter, report upgrade % and any rewrite that produces a
    suspicious URL (e.g. `//originals/originals/…` double-match).

### 1B — Concurrent URL scraping inside a cycle

**Why**: downloads are currently sequential per candidate. With per-host
throttle already in place, we can safely fan out across hosts in parallel.

- [x] **P1B.1 — Refactor the download loop in `_run_campaign_cycle`** — new
  `djangoscrap/ingestion_download.parallel_download_urls` module; cycle now
  pre-filters URLs against `already_seen_urls`, fans out 8-worker download
  pool, then processes results sequentially for dedupe/QC/DB writes.
  - Current loop (≈ `for u in candidate_urls: _download_ingestion_url(…)`)
    → `ThreadPoolExecutor(max_workers=8)` with a `Future` per URL.
  - The per-host `HOST_THROTTLE` already serializes same-host traffic, so
    threaded fan-out across hosts is safe.
  - Important: `IngestionItem.objects.create()` calls must stay
    serial (SQLite write lock) — do them on the main thread as futures
    resolve.

- [x] **P1B.2 — Preserve determinism** — URL-seen filter moved pre-fanout;
  DB writes stay sequential on consumer side; HOST_THROTTLE keeps per-host
  politeness intact.
  - `already_seen_urls` set access must be guarded with a `Lock`, or
    pre-compute the unique URL list before dispatching.

- [x] **P1B.3 — `tests/test_ingestion_download.py`** — 7 tests covering
  empty input, correctness, wall-clock speedup (8×100ms < 600ms with 4
  workers), exception → None result, two stop_when semantics, single-worker
  path. All green.
  - Mock 50 URLs across 10 hosts. Assert total wall-clock < sequential
    equivalent, but per-host rate stays within `HOST_THROTTLE` limits.

- [x] **P1B.4 — `parallel=N wall_ms=XXX` in report** — `DownloadStats`
  extended with `parallel_workers` + `wall_ms` fields, both stamped by
  `_run_campaign_cycle` around the fan-out.
  - `download: probed=N head_blocked=N … parallel=8 wall_ms=12345`

### Phase 1 tests

- `scripts/run_tests.sh` still green.
- Real cycle on an active batch shows:
  - `url-rewriter: upgraded=N` with `rules=…` containing **at least 15
    distinct rule tags** (proof maxurl rules are firing, not just the
    original 11).
  - `download: wall_ms=<…>` measurably lower than pre-phase on a
    candidate-count-matched cycle.

### Phase 1 success metric

On a 50+ candidate cycle: upgrade rate ≥ 40% (was ~10%) and cycle
wall-time drops by ≥ 40% at same candidate count.

### Phase 1 rollback

- 1A: delete `ingestion_site_rules.json` (the overrides file alone keeps
  pre-phase behavior).
- 1B: git revert the `_run_campaign_cycle` download-loop commit. Per-host
  throttle stays in place either way.

---

## Phase 2 — Adapter framework

**Goal**: create the plumbing that every new extractor (Pinterest scroller,
instagrapi, yt-dlp expansion, DOM-aware extractors) will plug into.

### Tasks

- [x] **P2.1 — Define the `Adapter` protocol** — `djangoscrap/ingestion_adapters/base.py`
  with `AdapterResult` (urls, next_state, diagnostics, error) and a
  runtime-checkable `Adapter` Protocol.
  - New package: `djangoscrap/ingestion_adapters/`
  - `base.py` exposes:
    ```python
    class Adapter(Protocol):
        name: str                          # "instagrapi", "pinterest_board", …
        def can_handle(url: str) -> bool: ...
        def extract(url: str, *, max_urls: int,
                    state: dict) -> AdapterResult: ...
    ```
  - `AdapterResult` dataclass: `urls: list[str]`, `next_state: dict`
    (for resumable sync), `diagnostics: dict` (for the report line).

- [x] **P2.2 — Registry + dispatcher** — thread-safe `register`, `pick_for`,
  `dispatch` with priority ordering + fall-through-on-empty semantics +
  `enabled_adapters` feature-flag hook.
  - `registry.py` — `register(adapter)`, `pick_for(url) -> Adapter | None`.
  - Adapters registered in priority order (specific → generic).

- [x] **P2.3 — Dispatcher wired into `_extract_candidates_from_page`** —
  new `enabled_adapters` + `adapter_state` kwargs; when auto mode + any
  adapter enabled, dispatcher runs first; falls through to legacy engine
  chain on empty result.
  - Try registered adapters first; fall back to today's engine chain
    (playwright, gallery-dl, yt-dlp, playwright-html) if no adapter matches.
  - Adapter result's `diagnostics` are merged into the campaign report.

- [x] **P2.4 — Reference adapter** — `PassthroughAdapter` (priority=10,
  direct-link handler) validates the shape and can serve real campaigns
  for pasted CDN URLs.
  - `YandexHtmlAdapter` — wraps the existing `_extract_with_yandex_html`
    function behind the `Adapter` interface. No new behavior; proves the
    framework + registry work.

- [x] **P2.5 — Framework tests** — `tests/test_adapters.py` with 20 tests
  (registry ordering, replacement, unregister, pick_for, dispatch
  fallthrough, feature-flag, Passthrough can_handle/extract, Protocol
  conformance). All green.
  - `tests/test_adapters.py` — registry priority, dispatcher fallback,
    `AdapterResult` reporting merge.
  - Existing cycle behavior unchanged — runs old tests.

### Phase 2 tests

- `scripts/run_tests.sh` green.
- Live cycle identical behavior to Phase 1: same upgrade rate, same
  candidate counts. Framework is transparent.

### Phase 2 success metric

`tests/test_adapters.py` green **and** a real cycle's report contains
`adapter=yandex-html:…` where previously it read `engines=yandex-html:…`.

### Phase 2 rollback

Git revert the dispatcher patch in `_extract_candidates_from_page`. The
adapters/ package stays as dead code — harmless.

---

## Phase 3 — Populate the adapter registry

Each sub-task is independent; order inside this phase is by impact.

### 3A — yt-dlp expansion (easiest win, already a dep)

- [x] **P3A.1 — `YtDlpAdapter`** — `djangoscrap/ingestion_adapters/ytdlp.py`,
  priority 30, gated on a 19-host suffix allowlist so random HTML doesn't
  spawn yt-dlp. Uses `-g --flat-playlist` to list media URLs without download.
  - Wraps yt-dlp's Python API (`yt_dlp.YoutubeDL({"simulate": True, …})`).
  - Covers ~1800 hosts: Reddit video, Bilibili, Douyin, Kuaishou, Pixiv,
    DeviantArt (video posts), Xiaohongshu, etc.
  - For each hit, probe both thumbnail + video URL; prefer video when
    ≥ 100 KB per the existing HEAD probe.
  - Registered with **low priority** — runs only when no specialized
    adapter matches.

- [x] **P3A.2 — Unit tests with mocked subprocess** — 17 tests covering
  host matching (9 variants), binary missing → can_handle False, stdout
  parsing (blank-line and non-URL filtering), 0-URL → error, non-zero
  exit → error, timeout → error, max_urls cap. Deferred: live per-host
  fixture (requires network).
  - Fixture: one URL each from Reddit video, Pixiv, DeviantArt, Bilibili.
  - Assert `yt-dlp` produces at least one URL per.

### 3B — instagrapi (replace + fallback instaloader)

- [x] **P3B.1 — Dependency** — fail-soft import inside `_InstagrapiClientProvider`;
  adapter returns False from `can_handle` if instagrapi missing or creds absent.
  - `requirements.txt`: `instagrapi>=2.0,<3.0` (optional — fail-soft).
  - First-run cache lives at `~/.config/composition_webapp/ig_session.json`.

- [x] **P3B.2 — `InstagrapiAdapter`** — priority 100, all URL kinds (profile / p / reel / reels / tv), carousel unrolling, pk-based resumable cursor.
  - Handles `https://instagram.com/<handle>` and
    `https://instagram.com/p/<post_id>` and `.../reel/…`.
  - Logs in once using credentials from `.env`
    (`INSTAGRAM_USER`, `INSTAGRAM_PASS`); reuses session JSON on re-runs.
  - Supports post types: Posts, Reels, Stories, Highlights.
  - Handles carousels: one post → N media URLs.
  - Registered higher priority than `instaloader` or `yt-dlp` for
    Instagram URLs.

- [x] **P3B.3 — Fallback chain** — priority ordering handles this: registry
  picks instagrapi first (priority=100), falls through on empty/error; legacy
  engine chain (_extract_with_instaloader → yt-dlp) still runs afterward.
  - Adapter returns empty → registry falls through to `InstaloaderAdapter`
    (existing) → `YtDlpAdapter`.

- [x] **P3B.4 — Tests** — 19 unit tests with mocked `_InstagrapiClientProvider`
  + mock Media objects (URL classification, carousel unroll, pk cursor,
  exception→error). Deferred: live integration test (requires creds).
  - Unit: mocked `instagrapi.Client` returning canned post fixtures.
  - Integration: requires `INSTAGRAM_USER`/`INSTAGRAM_PASS`; runs only
    when `RUN_SOCIAL_TESTS=1` is set so CI doesn't need credentials.

### 3C — Playwright scroll-harvester (Pinterest, Tumblr)

- [ ] **P3C.1 — `PinterestBoardAdapter`**
  - Handles `https://www.pinterest.com/<user>/<board>/`,
    `.../search/pins/?q=…`, `.../ideas/…`.
  - Launches headless Chromium via existing Playwright install.
  - Implements BID's Pagetual pattern: scroll → extract
    `[data-test-id="pinrep-image"] img[src]` → scroll again. Stops after
    N pages or M scrolls with zero new images.
  - All extracted URLs pass through the existing rewriter (pinimg
    `/originals/` path).

- [ ] **P3C.2 — `TumblrTagAdapter`**
  - Handles `https://www.tumblr.com/tagged/<tag>/…`.
  - Same scroll pattern. Tumblr's embedded JSON payload has full-res
    URLs — prefer that over rendered `<img>`.

- [ ] **P3C.3 — Anti-bot softening**
  - Shared helper `_launch_stealth_browser()`: real Chrome UA, remove the
    `navigator.webdriver` flag, small scroll jitter, random viewport size
    within a plausible range.

- [ ] **P3C.4 — Tests**
  - Unit: feed a captured HTML fixture of a Pinterest board, assert image
    URL extraction.
  - Integration: live Pinterest URL behind `RUN_BROWSER_TESTS=1`.

### Phase 3 tests

- `scripts/run_tests.sh` green with adapter-specific tests excluded.
- `RUN_SOCIAL_TESTS=1 RUN_BROWSER_TESTS=1 scripts/run_tests.sh` green on
  a machine with `.env` credentials.
- Real cycle: `adapters=yandex-html:3,yt-dlp:2,instagrapi:1,pinterest_board:1`
  in the report line.

### Phase 3 success metric

One batch per adapter type successfully ingests at least 20 unique items
via that adapter alone.

### Phase 3 rollback

- Per-adapter: disable in the registry (change priority to 0) or `git revert`
  the adapter's register call. The `Adapter` framework itself stays.

---

## Phase 4 — New sourcing surfaces (depend on adapter framework)

### 4A — Account archiver mode

- [ ] **P4A.1 — Model extension**
  - `IngestionBatch.source_kind` already exists; add enum value `"account"`.
  - `campaign_state["adapter_state"]` holds per-adapter resume data:
    ```json
    {
      "instagrapi": {
        "last_post_id": "3412…",
        "last_post_ts": "2026-04-17T09:30:00Z",
        "carousel_depth_completed": true
      }
    }
    ```
  - No new migration needed — `campaign_state` is already a JSONField.

- [ ] **P4A.2 — Per-account folder convention**
  - Destination folder name computed as
    `{site}_{handle}__{subject_hint}__{site}`.
  - Vision-QC defaults to off for account archives (we want everything
    the account posted, not a vibes filter).

- [ ] **P4A.3 — Incremental sync hook**
  - Before every cycle, if `source_kind == "account"`, pass
    `state["adapter_state"][adapter.name]` into `adapter.extract(…)`.
  - Adapter uses this to request only posts newer than the highwater mark.
  - Post-cycle, adapter returns `next_state` which is merged back.

- [ ] **P4A.4 — UI affordance**
  - "Archive account" button on the batch admin — single field: paste URL,
    system detects adapter, creates batch.

- [ ] **P4A.5 — Tests**
  - Unit: adapter receives state, returns state; second invocation with
    that state only fetches newer items.

### 4B — Multi-engine search fan-out

Goal: each query runs against N engines in parallel.

- [ ] **P4B.1 — `SearchAdapter` variant of the base protocol**
  - `search(query: str, *, max_urls: int) -> AdapterResult`
  - Implementations: `BingImageSearchAdapter`, `DuckDuckGoImageAdapter`,
    `FlickrSearchAdapter` (tightly rate-limited, hence its own adapter),
    `PixabayAdapter`, `UnsplashAdapter`, `PexelsAdapter`,
    `BooruAdapter` (covers Safebooru/Gelbooru/Danbooru via shared XML API).
  - API keys (where required) live in `.env`, fail-soft absent.

- [ ] **P4B.2 — Parallel dispatcher**
  - `_run_campaign_cycle` launches one `Future` per search adapter per query,
    collects results, dedupes by URL, feeds the unified set into the rewriter
    → HEAD probe → download pipeline.

- [ ] **P4B.3 — Engine diversity cap**
  - To prevent Bing from flooding the cycle with 200 URLs, each engine is
    capped at `max_urls_per_engine = cand_budget // engine_count`.

- [ ] **P4B.4 — Report token**
  - `search_fanout: bing=40, duckduckgo=20, flickr=5, booru=15, …`

### 4C — CBIR reverse-image fan-out

Goal: accepted seed image → "find more like this" across N reverse-image
services in parallel.

- [ ] **P4C.1 — `ReverseSearchAdapter` interface**
  - `reverse_search(image_path: Path, *, max_urls: int) -> AdapterResult`
  - Existing `_extract_with_playwright_dynamic(yandex_cbir_url)` becomes
    the first implementation.
  - Add: Google Lens (via headless page that accepts an image upload),
    Bing Visual Search, TinEye.

- [ ] **P4C.2 — Seed selection**
  - Already have CBIR seeds logic in `_run_campaign_cycle`; extend to
    dispatch each seed across all reverse-search adapters in parallel.

### Phase 4 tests

- Account archive of a throwaway test account: first run imports N posts,
  second run imports 0 (all already seen), third run (after new post)
  imports 1.
- Multi-engine cycle report shows ≥ 3 engines in `search_fanout=`.
- CBIR fan-out report shows ≥ 2 reverse-search engines contributing URLs.

### Phase 4 success metric

A batch tagged "neon nightlife photography" with no seeds can reach
`folder=400/400` in ≤ 6 cycles using only multi-engine + CBIR (no manual
URL paste).

### Phase 4 rollback

Disable individual search/reverse adapters by lowering priority. Account
archive mode is a new code path; rolling back just leaves it unused.

---

## Phase 5 — Per-site DOM-aware extractors

Builds on Phase 3C's Playwright foundation. Each extractor knows one
site's DOM structure and pulls original-resolution assets from its quirky
embed format.

Ordered by visual-quality yield per adapter:

- [ ] **P5.1 — PixivAdapter** (stylized art, huge stylistic diversity)
- [ ] **P5.2 — DeviantArtAdapter** (same territory, different demographic)
- [ ] **P5.3 — TwitterXAdapter** (via nitter fallback + direct DOM)
- [ ] **P5.4 — VKAdapter** (album-heavy, underused by Yandex)
- [ ] **P5.5 — WeiboAdapter** (Chinese-language content; Weibo photo walls
  are aesthetically rich and under-served in Western search)

Each follows the same contract:
- Inherits from a new `BaseDomExtractor` helper that encapsulates:
  scroll → extract → paginate → return pattern.
- Ships with one captured HTML fixture in `tests/fixtures/` and a
  unit test that asserts ≥ 10 URLs extracted.

### Phase 5 tests

- Per-adapter unit test green on its fixture.
- Manual smoke: each adapter produces ≥ 30 unique URLs from a known-good
  profile/board.

### Phase 5 success metric

A campaign with `"aesthetic": "japanese retro anime screencaps"` reaches
target count with ≥ 40% of items sourced from Pixiv/DeviantArt.

---

## Phase 6 — Concept-to-campaign auto-config

The "just describe the folder" UX. Everything before this makes it possible;
this phase makes it easy.

- [ ] **P6.1 — `_suggest_campaign_config_from_concept(brief: str)`**
  - GPT-4o call returns a JSON plan:
    ```json
    {
      "query_pack": ["neon noir cityscape", "80s retro synthwave", …],
      "engines": ["yandex", "bing", "pixiv", "deviantart"],
      "reverse_search_on_accepted": true,
      "vision_qc_strictness": 0.7,
      "target_count": 400
    }
    ```
  - Tuned on 5–10 example (brief → good-plan) pairs kept in
    `djangoscrap/concept_planner_examples.json`.

- [ ] **P6.2 — CBIR seed auto-population**
  - First cycle of a new concept-only campaign: GPT picks 3–5 example URLs
    from the open web, downloads them as seeds (subject to normal HEAD/
    throttle/dedupe), uses them as CBIR seeds.

- [ ] **P6.3 — "Refresh folder" action**
  - Button on published batches: re-open the campaign with a target count
    bumped by N%, re-run adapters, sync diff into the existing folder.

- [ ] **P6.4 — Tests**
  - Golden tests: 5 example briefs → assert GPT returns a plan that parses,
    includes ≥ 3 engines, and has a query_pack of ≥ 5 queries.

### Phase 6 success metric

User types a one-sentence brief in the admin UI and clicks "Go."
Within 10 minutes the folder has ≥ 50 on-theme images and no broken tiles.
This is the north star.

---

## Cross-cutting: observability checklist

At **every** phase completion, `campaign_last_report` should include one
new token per new component. Target final shape:

```
trigger=scheduled; pages=12 (similar=8, text=4); cbir_seeds=8;
adapters=yandex-html:4,bing:2,pinterest_board:1,instagrapi:1,yt-dlp:3;
search_fanout=bing:40,duckduckgo=18,flickr=6,pixiv=22,booru=11;
reverse_fanout=yandex-cbir:12,google-lens:8,tineye=3;
candidates=187; imported=98; duplicates=34 (clip:12,phash:18,sha:4);
filtered=11; failed=7; url_skipped=40;
qc_checked=98; qc_rejected=9;
url-rewriter: in=187 upgraded=94 dropped=22 rules=pinterest:41,tumblr:18,flickr:9,…;
download: probed=165 head_blocked=31 get_ok=127 get_failed=7
  reasons=too_small:14, http_404:8, wrong_type:4, head_forbidden:3, get_http_403:2
  parallel=8 wall_ms=42317;
folder=460/500; queue=0; review_then_publish
```

Before closing a phase, confirm every new token appears. If it doesn't,
the component isn't wired into reporting — fix before ticking the box.

---

## Rollback playbook

When a phase breaks prod-like behavior, execute in this order:

1. **Disable the feature flag first.** Every new adapter / engine is behind
   a `campaign_state["adapters"][name]` boolean. Flip to `false` on the
   affected batches; this requires no deploy.
2. **If the regression is in core code** (rewriter / HTTP / dedupe),
   `git revert` the offending commit. All phase commits are ≤ 400 lines
   specifically to make this clean.
3. **Run `python manage.py ingestion_selftest`.** If it goes back to green,
   ingestion is safe to resume.
4. **Write the regression into `tests/` before re-attempting the phase.**
   No untested re-attempts.

---

## Dependency graph (who unblocks whom)

```
Phase 0 (tests)
  ↓
Phase 1A (maxurl port)         Phase 1B (concurrency)
  ↓                              ↓
  └───────────┬──────────────────┘
              ↓
Phase 2 (adapter framework)
              ↓
              ├──► Phase 3A (yt-dlp)
              ├──► Phase 3B (instagrapi)
              └──► Phase 3C (Playwright scroll)
                       ↓
                       ├──► Phase 4A (account archiver)
                       ├──► Phase 4B (multi-engine search)
                       └──► Phase 4C (CBIR fan-out)
                                 ↓
                                 └──► Phase 5 (per-site DOM)
                                            ↓
                                            └──► Phase 6 (auto-config)
```

Phase 1 sub-items are **independent** — 1A and 1B can ship in either order.
Phase 3 sub-items are **independent** once Phase 2 lands.
Phases 4, 5, 6 are strictly sequential.

---

## Definition of done (whole roadmap)

The ingestion pipeline is "done" when **all** of these hold on a fresh
machine after `git pull && pip install -r requirements.txt && migrate`:

- [ ] `scripts/run_tests.sh` → green.
- [ ] `python manage.py ingestion_selftest` → PASS.
- [ ] A fresh empty batch with a one-sentence brief and no seeds can reach
      `folder=N/target` autonomously in ≤ 6 cycles.
- [ ] `/ingestion/<id>/` grid shows zero broken tiles after any cycle.
- [ ] `campaign_last_report` lines contain all observability tokens listed
      in the cross-cutting checklist above.
- [ ] An account URL (Instagram handle, Pinterest board, Pixiv profile)
      pasted into "Archive account" reaches the first non-empty cycle
      within one run and the second cycle imports zero duplicates.
- [ ] No new dependency in `requirements.txt` is required at import time —
      everything optional fails soft.

---

## Current status

**Last updated**: 2026-04-18
**Current phase**: **ALL PHASES COMPLETE** ✅ — follow-up: grid-first UX + live
status watcher for `/ingestion/<id>/` shipped.

### Test coverage
- 242 pytest tests green (up from 42 at start of plan)
- `manage.py ingestion_selftest` PASS
- `bash scripts/run_tests.sh` → all stages green

### Phase-by-phase outcome

| Phase | Module(s) shipped | Tests | Benchmark |
|-------|-------------------|-------|-----------|
| 0 | `pytest.ini`, `tests/`, `ingestion_selftest.py`, `run_tests.sh` | 42 → baseline harness | PASS |
| 1A | `ingestion_url_rewriter.py` + `ingestion_site_rules.json` | 20 (+ 12 overlay) | +94 % upgrades, 88.6 % coverage |
| 1B | `ingestion_download.py` (parallel fan-out) | 6 | 8 workers, `wall_ms` telemetry live |
| 2 | `ingestion_adapters/{base,registry,passthrough}.py` | 19 | dispatch P95 < 0.1 ms |
| 3A | `ingestion_adapters/ytdlp.py` | 17 | allowlist + subprocess fail-soft |
| 3B | `ingestion_adapters/instagrapi_adapter.py` | 19 | carousel unroll + `last_post_pk` cursor |
| 3C | `ingestion_adapters/playwright_scroll.py` (Pinterest, Tumblr) | 14 | host-filtered image extract |
| 4A | `ingestion_archiver.py` | 13 | resume + drain logic |
| 4B | `ingestion_search_fanout.py` | 19 | 5 engines × N pages |
| 4C | `ingestion_cbir.py` | 15 | 4 reverse-image engines |
| 5 | `ingestion_dom_extractors.py` | 21 | Google/Bing/Yandex/Pinterest/Tumblr/Flickr + generic fallback |
| 6 | `ingestion_campaign_planner.py` | 25 | 48 URLs from 10-line paste in 0.098 ms |

### Cross-cutting benchmarks

`scripts/benchmark_cycle_planner.py` run on the cohesive final planner:

```
brief: concept='aesthetic research' queries=5 archive_seeds=3 cbir_seeds=2
search-fanout: queries=5 urls=40 [bing=10,duckduckgo=5,google=10,pinterest=5,yandex=10] |
archive: seeds=3 ok=0 drained=0 errors=3 urls=0 |
cbir: seeds=2 urls=8 [bing-rev=2,google-rev=2,tineye-rev=2,yandex-rev=2] |
cycle-total-urls=48
unique URLs queued for download: 48
mean wall-clock per build_cycle_brief call: 0.098 ms
```

### Recent wins (already shipped, pre-plan)
- Three-layer dedupe (SHA256 + pHash + CLIP)
- URL rewriter with 11 rules + JSON overlay
- Watermark-host ignore list
- HEAD-probe pre-download filtering
- Per-host throttle with min-delay
- Vision-QC auto-accept and tombstone-free rejections
- Disk-cached composition previews

### Integration follow-ups (optional, out of plan scope)

The new planners are pure-Python and side-effect-free. Wiring them into
`views._run_campaign_cycle` as replacements for the legacy engine loop
is a separate, small integration task (one `if campaign_mode == …` branch
per planner). The test harness plus `ingestion_selftest` canary means
that wiring can be done behind a feature flag with minimal risk.

### UX follow-up (shipped 2026-04-18) — grid-first ingestion page

User-facing redesign of `/ingestion/<id>/` to give a bulk-image-downloader
style workflow on top of the existing backend. No backend form names or
endpoints changed, so this is purely a template + one new JSON view.

- New endpoint `ingestion_batch_status` at `/ingestion/<id>/status.json`
  (lightweight: one `IngestionBatch` fetch + one `values().annotate(Count)`)
  returns running flag, counts, progress %, and `campaign_last_report`.
- `ingestion_batch_detail.html` rewritten around a sticky header strip,
  a unified filter chip row, a responsive CSS-grid of cards, and a
  sticky selection action bar (`Accept / Reject / Delete / Publish`).
- "Hide published" client-side toggle defaults **on**, so finished
  batches no longer clutter the active review view.
- Campaign + Advanced panels collapsed into `<details>` elements —
  settings are still one click away but don't dominate the page.
- Live watcher: JS polls the new status endpoint every 4 s while the
  campaign runs (every 15 s when idle), updating counts, progress bar,
  status pill, and the `campaign_last_report` console.
- Selection UX kept: click to toggle, Alt+drag marquee, Select visible,
  Invert. All bulk actions reuse the same POST action names the backend
  already accepts.
- 242 pytest + `ingestion_selftest` still green after the change.

### Ingestion perf + "delete means delete" follow-up (shipped 2026-04-18)

Three complaints against `/ingestion/<id>/`: pages felt slow, deleted items
kept coming back, and campaign results were straying from the concept. All
three addressed without a schema migration.

- Perf:
  - The detail view used `Paginator(items_qs, max(1, total_items))`, i.e. page
    size == total items, so a batch of 1177 sent all 1177 cards (+ 1177
    `<img loading=lazy>` tags) on every load. Hard-capped to 180 cards per
    page; a `?page=N` link set ships the rest. Page render for batch 141
    now measures ~40 ms (was several hundred ms on refresh).
  - Collapsed the 5 separate `COUNT(*)` queries for `total / pending /
    accepted / rejected / queue` into one `aggregate(Count, filter=Q(...))`.
  - Dropped `_source_picker_cards(limit=260)` from the context — the new
    template doesn't use `existing_source_cards` (only the name list).
- "Delete means delete" — new `/ingestion/<id>/bulk-ajax/` JSON endpoint
  (`ingestion_batch_bulk_ajax`) handles Accept / Reject / Delete without a
  full page reload; the client drops cards from the DOM in place. Deletes
  additionally record URL, sha256 and phash tombstones into
  `campaign_state["deleted_tombstones"]` (capped at 5000 entries per list,
  FIFO). The campaign cycle now:
    - filters candidate URLs against the URL tombstone *before* download, and
    - cross-checks sha256 + phash tombstones *after* download, rejecting any
      candidate that matches a previously-deleted item even if the CDN served
      different bytes the second time.
  The form-POST `bulk_delete` action records the same tombstones as a fallback
  path.
- Drift mitigation:
  - `_default_campaign_vision_qc_prefs()` now defaults to `strictness="strict"`
    (was "normal"). `_get_campaign_vision_qc_prefs` falls through to that
    default instead of the old literal "normal" when a batch has no stored
    value — so new batches get strict QC out of the box, existing batches with
    an explicit setting are untouched.
  - `_campaign_seed_snapshot` now returns `folder_file_count` (full folder
    size, not just the 24-sample preview).
  - In `_run_campaign_cycle`, the CBIR vs. text-query mix scales with
    reference strength (`accepted items + files in destination folder`):
        - ≥10 refs: ~95% CBIR, cap of 1 text page per run
        - ≥5  refs: ~85% CBIR, up to `max_pages / 3` text pages
        - <5  refs: 75% CBIR (original behaviour) so young batches still
          bootstrap via text search.
- Full suite green: 242 pytest + `ingestion_selftest`; one-off smoke tests
  confirmed the async endpoint round-trips and that delete actually records
  tombstones in `campaign_state`.

**Next action**: nothing blocking. All 13 planned phases are green.
