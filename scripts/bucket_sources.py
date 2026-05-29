"""Source adapters for bucket_build.py.

Each adapter exposes:

    def fetch(params: dict, *, since_count: int) -> Iterable[Candidate]

where Candidate is a dict-like with at least:

    {
      "url":            str,            # direct image/video URL
      "source_type":    str,            # "reddit" / "wikimedia" / ...
      "source_subkey":  str,            # subreddit name / category / channel slug
      "source_url":     str,            # parent page (post URL, etc.)
      "title":          str,            # optional, for prompts/captions later
      "score":          int,            # source-side popularity (optional)
      "ext_hint":       str,            # ".jpg" / ".mp4" hint when known
    }

Adapters yield candidates lazily so the orchestrator can stop once it has
enough. They do NOT download — that's the orchestrator's job. They DO
filter to safe direct URLs (no link aggregators, no html pages).

Implemented:
    reddit_aesthetic   — uses Arctic Shift HTTP API (no Reddit key needed)
    wikimedia_commons  — Wikimedia API category traversal
    gallery_dl         — wraps the gallery-dl CLI for any of its 250+ sources
                         (Pinterest, Are.na, Tumblr, Twitter, Flickr, ...)

Stubbed (clear interface, easy to fill in):
    yandex_reverse, lora_generate, tiktok_hashtag
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

UA = "compositions-bucket-builder/0.1"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ext_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    return ext


def _is_direct_media(url: str, allow_video: bool = False) -> bool:
    ext = _ext_from_url(url)
    if ext in IMAGE_EXTS:
        return True
    if allow_video and ext in VIDEO_EXTS:
        return True
    if "i.redd.it" in url or "i.imgur.com" in url:
        return True
    return False


def _http_json(url: str, timeout: float = 30.0, retries: int = 3) -> dict | list:
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"http_json failed: {url} ({last_err})")


# ---------------------------------------------------------------------------
# Reddit adapter (Arctic Shift)
# ---------------------------------------------------------------------------

def fetch_reddit(params: dict, *, allow_video: bool = False) -> Iterable[dict]:
    """params:
        subreddits: list[str]                  required
        min_score: int                         default 25
        limit_per_sub: int                     default 1000
        since: "YYYY-MM-DD"                    optional
        after_ts: int                          optional cursor
    """
    base = "https://arctic-shift.photon-reddit.com/api/posts/search"
    subs = params.get("subreddits") or []
    min_score = int(params.get("min_score", 25))
    limit = int(params.get("limit_per_sub", 1000))
    since_ts = 0
    if params.get("since"):
        from datetime import datetime, timezone
        since_ts = int(datetime.strptime(params["since"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    cursor = int(params.get("after_ts", since_ts))

    for sub in subs:
        n_yielded = 0
        sub_cursor = cursor
        while n_yielded < limit:
            qs = urllib.parse.urlencode({
                "subreddit": sub, "limit": 100, "sort": "asc", "after": sub_cursor,
            })
            try:
                payload = _http_json(f"{base}?{qs}")
            except Exception:
                break
            items = payload.get("data") or []
            if not items:
                break
            page_max_ts = sub_cursor
            for d in items:
                ts = int(d.get("created_utc") or 0)
                if ts <= sub_cursor:
                    continue
                page_max_ts = max(page_max_ts, ts)
                score = int(d.get("score") or 0)
                if score < min_score:
                    continue
                # OP image extraction (mirrors scrape_reddit_full.extract_image_urls)
                urls: list[str] = []
                u = d.get("url") or d.get("url_overridden_by_dest") or ""
                if u and _is_direct_media(u, allow_video=allow_video):
                    urls.append(u)
                if d.get("is_gallery") and d.get("media_metadata"):
                    for it in (d["media_metadata"] or {}).values():
                        if it.get("status") == "valid" and it.get("s"):
                            src = it["s"].get("u") or it["s"].get("gif") or ""
                            if src:
                                urls.append(src.replace("&amp;", "&"))
                if not urls:
                    preview = d.get("preview") or {}
                    images = preview.get("images") or []
                    if images:
                        src = (images[0].get("source") or {}).get("url") or ""
                        if src:
                            urls.append(src.replace("&amp;", "&"))
                if not urls:
                    continue
                for u in urls:
                    yield {
                        "url": u,
                        "source_type": "reddit",
                        "source_subkey": sub,
                        "source_url": f"https://reddit.com/r/{sub}/comments/{d.get('id')}",
                        "title": d.get("title") or "",
                        "score": score,
                        "ext_hint": _ext_from_url(u) or ".jpg",
                    }
                    n_yielded += 1
                    if n_yielded >= limit:
                        break
                if n_yielded >= limit:
                    break
            if page_max_ts == sub_cursor:
                sub_cursor += 1
            else:
                sub_cursor = page_max_ts
            if len(items) < 100:
                break


# ---------------------------------------------------------------------------
# Wikimedia Commons adapter
# ---------------------------------------------------------------------------

def fetch_wikimedia(params: dict, **_kw) -> Iterable[dict]:
    """params:
        categories: list[str]   e.g. ["Forests at night", "Dark forests"]
        limit: int              default 500
        recurse_depth: int      default 1
    """
    cats = params.get("categories") or []
    limit = int(params.get("limit", 500))
    depth = int(params.get("recurse_depth", 1))

    api = "https://commons.wikimedia.org/w/api.php"
    n_yielded = 0

    def list_cat(cat: str, sub_depth: int):
        nonlocal n_yielded
        if n_yielded >= limit or sub_depth < 0:
            return
        cont = ""
        while n_yielded < limit:
            params_q = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": f"Category:{cat}",
                "cmlimit": "100",
                "cmtype": "file|subcat",
                "format": "json",
            }
            if cont:
                params_q["cmcontinue"] = cont
            url = f"{api}?{urllib.parse.urlencode(params_q)}"
            try:
                d = _http_json(url)
            except Exception:
                return
            members = (d.get("query") or {}).get("categorymembers") or []
            for m in members:
                title = m.get("title", "")
                if title.startswith("Category:") and sub_depth > 0:
                    list_cat(title.split(":", 1)[1], sub_depth - 1)
                elif title.startswith("File:"):
                    file_url = _commons_file_url(title)
                    if file_url and _is_direct_media(file_url):
                        yield {
                            "url": file_url,
                            "source_type": "wikimedia",
                            "source_subkey": cat,
                            "source_url": f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title)}",
                            "title": title,
                            "score": 0,
                            "ext_hint": _ext_from_url(file_url) or ".jpg",
                        }
                        n_yielded += 1
                        if n_yielded >= limit:
                            return
            cont = (d.get("continue") or {}).get("cmcontinue", "")
            if not cont:
                return

    for cat in cats:
        yield from list_cat(cat, depth)
        if n_yielded >= limit:
            break


def _commons_file_url(title: str) -> str | None:
    """Resolve a Commons File:Title to its direct media URL."""
    api = "https://commons.wikimedia.org/w/api.php"
    qs = urllib.parse.urlencode({
        "action": "query", "titles": title,
        "prop": "imageinfo", "iiprop": "url|mime|size",
        "format": "json",
    })
    try:
        d = _http_json(f"{api}?{qs}")
    except Exception:
        return None
    pages = (d.get("query") or {}).get("pages") or {}
    for _, page in pages.items():
        info = (page.get("imageinfo") or [])
        if info:
            return info[0].get("url")
    return None


# ---------------------------------------------------------------------------
# gallery-dl wrapper (Pinterest, Are.na, Tumblr, Twitter, Flickr, ArtStation, ...)
# ---------------------------------------------------------------------------

def fetch_gallery_dl(params: dict, *, dest_root: str = "/tmp/gdl_staging", **_kw) -> Iterable[dict]:
    """params:
        urls: list[str]                 page/board/account URLs
        limit_per_url: int              default 500
        cookies_from_browser: str       optional, e.g. "firefox" / "chrome"
    """
    if not shutil.which("gallery-dl"):
        return  # nothing to yield; caller can warn
    urls = params.get("urls") or []
    limit = int(params.get("limit_per_url", 500))
    cookies = params.get("cookies_from_browser")

    for url in urls:
        # gallery-dl will write files to dest_root; we capture them via -j (JSON output).
        # Using --simulate + --print to enumerate target URLs without downloading.
        cmd = [
            "gallery-dl",
            "--range", f"1-{limit}",
            "--simulate",
            "--print", "{url}\t{filename}\t{extension}",
        ]
        if cookies:
            cmd += ["--cookies-from-browser", cookies]
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception:
            continue
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            media_url, fname, ext = parts[0], parts[1], parts[2]
            if not _is_direct_media(media_url):
                # gallery-dl sometimes prints page URLs; trust the extension hint.
                ext_h = "." + ext.lower() if ext else ""
                if ext_h not in IMAGE_EXTS and ext_h not in VIDEO_EXTS:
                    continue
            yield {
                "url": media_url,
                "source_type": "gallery_dl",
                "source_subkey": urllib.parse.urlparse(url).netloc,
                "source_url": url,
                "title": fname,
                "score": 0,
                "ext_hint": ("." + ext.lower()) if ext else _ext_from_url(media_url) or ".jpg",
            }


# ---------------------------------------------------------------------------
# Stubs — clear interfaces, fill in when needed
# ---------------------------------------------------------------------------

def fetch_pinterest_browse(params: dict, **_kw) -> Iterable[dict]:
    """Scrape a Pinterest board / profile / search page via headless Chromium.

    Pinterest is a goldmine for aesthetic-coherent imagery — boards are
    explicitly human-curated. This adapter opens a Pinterest URL in
    Playwright-driven Chrome, dismisses cookie banners, scrolls to lazy-load
    pins, and yields the high-resolution image URLs.

    params:
        urls: list[str]            Pinterest URLs to scrape
        limit_per_url: int         default 200, per URL
        scroll_passes: int         default 30, how many scroll cycles
        scroll_delay_sec: float    default 1.5, between scrolls
        headless: bool             default True (uses 'new' headless mode)
        wait_after_load_sec: float default 3.0, initial wait after navigation

    Yields candidates with high-res `i.pinimg.com/originals/...` URLs (auto-
    upgraded from any sized variant).

    Notes:
        - Public boards work without auth. Some private/login-walled pages
          will fail or return only teaser content.
        - Pinterest's anti-bot is real but mild for moderate scraping.
          Throttle if you hit walls.
        - This is the prototype Playwright source — same shape (open URL,
          scroll, extract <img>) generalizes to Tumblr, Are.na, niche
          aesthetic blogs.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("    [pinterest_browse] playwright not installed; skipping.", flush=True)
        return

    urls = params.get("urls") or []
    limit = int(params.get("limit_per_url", 200))
    scroll_passes = int(params.get("scroll_passes", 30))
    scroll_delay = float(params.get("scroll_delay_sec", 1.5))
    headless = bool(params.get("headless", True))
    initial_wait = float(params.get("wait_after_load_sec", 3.0))

    if not urls:
        return

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        # Hide the navigator.webdriver flag — common stealth tweak
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        try:
            for board_url in urls:
                page = context.new_page()
                try:
                    page.goto(board_url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    print(f"    [pinterest_browse] goto failed for {board_url}: {e}", flush=True)
                    page.close()
                    continue

                page.wait_for_timeout(int(initial_wait * 1000))
                _pinterest_dismiss_overlays(page)

                seen: set[str] = set()
                last_count = 0
                stagnant = 0

                for i in range(scroll_passes):
                    page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
                    page.wait_for_timeout(int(scroll_delay * 1000))

                    new_urls = _pinterest_extract_pin_urls(page)
                    for u in new_urls:
                        if u in seen:
                            continue
                        seen.add(u)
                        if len(seen) > limit:
                            break

                    if len(seen) == last_count:
                        stagnant += 1
                        if stagnant >= 4:
                            break
                    else:
                        stagnant = 0
                    last_count = len(seen)
                    if len(seen) >= limit:
                        break

                # Yield results
                board_label = urllib.parse.urlparse(board_url).path.strip("/").split("/")[0] or "pinterest"
                for u in list(seen)[:limit]:
                    yield {
                        "url": u,
                        "source_type": "pinterest_browse",
                        "source_subkey": board_label,
                        "source_url": board_url,
                        "title": "",
                        "score": 0,
                        "ext_hint": _ext_from_url(u) or ".jpg",
                    }
                page.close()
        finally:
            context.close()
            browser.close()


def _pinterest_dismiss_overlays(page) -> None:
    """Best-effort dismissal of cookie banners / login walls."""
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        'button[aria-label="Accept all cookies"]',
        '[data-test-id="cookie-banner-accept-button"]',
        'button:has-text("Continue with email")',  # close login modal X
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=1500)
                page.wait_for_timeout(500)
        except Exception:
            continue
    # Try the X-button on any modal
    try:
        x = page.locator('[aria-label="Close"]').first
        if x.is_visible(timeout=1000):
            x.click(timeout=1000)
    except Exception:
        pass


def _pinterest_extract_pin_urls(page) -> list[str]:
    """Pull all i.pinimg.com URLs from current DOM, upgrade to /originals/."""
    raw = page.evaluate(
        "Array.from(document.querySelectorAll('img'))"
        ".map(i => i.src).filter(s => s && s.includes('i.pinimg.com'))"
    )
    out: list[str] = []
    seen: set[str] = set()
    for u in raw:
        upgraded = _upgrade_pinterest(u)
        if upgraded in seen:
            continue
        seen.add(upgraded)
        if _is_direct_media(upgraded):
            out.append(upgraded)
    return out


def fetch_yandex_reverse(params: dict, **_kw) -> Iterable[dict]:
    """Reverse-image-search seed images via SerpApi's yandex_images engine.

    Yandex is the best in the world at "find me images that visually look
    like this one." Where text-anchored search drifts (a 'dark forest' query
    returns wedding photos), Yandex returns actual visual neighbours.

    params:
        seed_paths: list[str]   absolute paths to seed images on disk
        limit_per_seed: int     default 50
        throttle_sec: float     default 1.0 between SerpApi calls
        upload_host: str        "catbox" (default) or "0x0"

    Each seed is briefly uploaded to a public host (catbox.moe by default,
    no API key) so SerpApi can fetch it. The remote copy is not deleted —
    catbox cycles inactive content out periodically.

    Requires SERPAPI_API_KEY in env. ~$0.005 per call on paid plans;
    Free plan has 250 calls/month.
    """
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("    [yandex_reverse] SERPAPI_API_KEY not set; skipping.", flush=True)
        return

    seed_paths = params.get("seed_paths") or []
    limit = int(params.get("limit_per_seed", 50))
    throttle = float(params.get("throttle_sec", 1.0))
    host = (params.get("upload_host") or "catbox").lower()

    last = 0.0
    for sp in seed_paths:
        sp = os.path.abspath(sp)
        if not os.path.isfile(sp):
            print(f"    [yandex_reverse] seed not found: {sp}", flush=True)
            continue
        try:
            public_url = _upload_for_yandex(sp, host=host)
        except Exception as e:
            print(f"    [yandex_reverse] upload failed for {sp}: {e}", flush=True)
            continue

        # Throttle SerpApi calls
        gap = throttle - (time.time() - last)
        if gap > 0:
            time.sleep(gap)
        last = time.time()

        sa_params = {
            "engine": "yandex_images",
            "url": public_url,           # SerpApi yandex_images param for reverse-search source
            "api_key": api_key,
        }
        try:
            results = _http_json(f"https://serpapi.com/search?{urllib.parse.urlencode(sa_params)}")
        except Exception as e:
            print(f"    [yandex_reverse] serpapi error for {sp}: {e}", flush=True)
            continue

        items = results.get("image_results") or results.get("images_results") or []
        # Some queries also return "similar_images" — append for breadth
        items = list(items) + list(results.get("similar_images") or [])
        n_yielded = 0
        for it in items:
            if n_yielded >= limit:
                break
            # SerpApi yandex_images returns objects with `original_image`
            # as a nested dict {link, width, height, ...}.
            oi = it.get("original_image")
            if isinstance(oi, dict):
                media_url = oi.get("link") or ""
            else:
                media_url = oi or it.get("link") or ""
            if not media_url:
                continue
            # Upgrade Pinterest sized URLs to /originals/ (much higher resolution)
            media_url = _upgrade_pinterest(media_url)
            # Skip AI-content / generation domains and AI-suggesting titles
            if _is_ai_domain(media_url) or _is_ai_domain(it.get("source") or ""):
                continue
            if _is_ai_title(it.get("title")):
                continue
            if not _is_direct_media(media_url):
                continue
            yield {
                "url": media_url,
                "source_type": "yandex_reverse",
                "source_subkey": os.path.basename(sp),
                "source_url": it.get("source") or it.get("link") or media_url,
                "title": it.get("title", "")[:240],
                "score": 0,
                "ext_hint": _ext_from_url(media_url) or ".jpg",
            }
            n_yielded += 1


_AI_DOMAINS = (
    "seaart.me", "seaart.ai", "civitai.com", "civit.ai", "openart.ai",
    "leonardo.ai", "leonardo.app", "midjourney.com", "tensor.art",
    "tensorart.com", "runwayml.com", "replicate.delivery", "replicate.com",
    "pixai.art", "novelai.net", "playground.com",
    "stablediffusionweb", "stablediffusion.online", "stablediffusion.fr",
    "promptbase", "promptomania", "lexica.art", "happyaccidents.ai",
    "dreamstudio.ai", "stablediffusion-fr", "deepai.org", "nightcafe.studio",
    "perchance.org", "krea.ai", "ideogram.ai", "magicstudio",
)


def _is_ai_title(title: str) -> bool:
    """Reject by title strings that smell AI-generated."""
    t = (title or "").lower()
    return any(s in t for s in (
        "stable diffusion", "midjourney", "ai generated", "ai-generated",
        "ai art", "generated by ai", "flux lora", "civitai",
        "ai image", "ai picture", "neural style",
    ))


def _is_ai_domain(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return any(d in host for d in _AI_DOMAINS)


def _upgrade_pinterest(url: str) -> str:
    """Pinterest hosts at multiple sizes (236x, 474x, 564x, 736x, originals).
    Force originals when possible — 5–10× resolution lift, same aesthetic."""
    try:
        if "i.pinimg.com" not in url:
            return url
        # Match `/<size>/` like /736x/, /474x/, /236x/, etc. and replace with /originals/
        return re.sub(r"i\.pinimg\.com/\d+x/", "i.pinimg.com/originals/", url)
    except Exception:
        return url


def fetch_yandex_direct(params: dict, **_kw) -> Iterable[dict]:
    """Reverse-image-search Yandex via pure HTTP — no SerpApi, no browser, no API key.

    The flow:
      1. POST seed file to yandex.com/images/search?rpt=imageview&format=json — returns
         a JSON blob with a `cbirId` token (Yandex's hosted reference for the image).
      2. GET yandex.com/images/search?cbir_id=<token>&rpt=imageview — returns the
         results-page HTML containing visually-similar matches inline.
      3. Regex-extract direct third-party image URLs from the HTML.

    No upload-to-public-host roundtrip (catbox not needed). No JavaScript rendering
    needed — all the result URLs are server-rendered into the HTML page.

    params:
        seed_paths: list[str]    absolute paths to seed images on disk
        limit_per_seed: int      default 60
        throttle_sec: float      default 2.0 between seeds (be polite)
        pages: int               default 1; bump for more results

    Caveats:
        - Yandex may layout-change selectors at any time. If extraction count drops
          to zero, this regex needs updating.
        - No proxy support; for high-volume use you'll hit rate limits / CAPTCHA.
        - Free, unlimited, no third-party dependency.
    """
    seed_paths = params.get("seed_paths") or []
    text_queries = params.get("text_queries") or []
    limit = int(params.get("limit_per_seed", 60))
    throttle = float(params.get("throttle_sec", 2.0))
    pages = int(params.get("pages", 1))

    # Build (label, fetch_html_fn) tuples
    tasks = []
    for sp in seed_paths:
        sp_abs = os.path.abspath(sp)
        if not os.path.isfile(sp_abs):
            print(f"    [yandex_direct] seed not found: {sp_abs}", flush=True)
            continue
        tasks.append(("seed", os.path.basename(sp_abs), sp_abs))
    for q in text_queries:
        tasks.append(("text", q, q))

    last = 0.0
    for kind, label, payload in tasks:
        gap = throttle - (time.time() - last)
        if gap > 0:
            time.sleep(gap)
        last = time.time()

        cbir_id = None
        if kind == "seed":
            try:
                cbir_id = _yandex_upload(payload)
            except Exception as e:
                print(f"    [yandex_direct] upload failed for {label}: {e}", flush=True)
                continue

        n_yielded = 0
        for page in range(pages):
            if n_yielded >= limit:
                break
            try:
                if kind == "seed":
                    html = _yandex_fetch_results(cbir_id, page=page)
                else:
                    html = _yandex_fetch_text(payload, page=page)
            except Exception as e:
                print(f"    [yandex_direct] results fetch failed: {e}", flush=True)
                break

            urls = _yandex_extract_image_urls(html)
            if not urls:
                # Layout change or empty results — log a hint
                if page == 0:
                    print(f"    [yandex_direct] zero URLs extracted for {os.path.basename(sp)} (rate limit or layout change)", flush=True)
                break

            for url in urls:
                if n_yielded >= limit:
                    break
                # Domain / quality screens
                if _is_ai_domain(url):
                    continue
                # Pinterest sized → originals upgrade
                url = _upgrade_pinterest(url)
                if not _is_direct_media(url):
                    continue
                yield {
                    "url": url,
                    "source_type": "yandex_direct",
                    "source_subkey": label[:80],
                    "source_url": url,
                    "title": "",
                    "score": 0,
                    "ext_hint": _ext_from_url(url) or ".jpg",
                }
                n_yielded += 1


def _yandex_upload(path: str) -> str:
    """POST a local file to Yandex's reverse-image endpoint, return cbir_id."""
    import urllib.request, mimetypes
    boundary = f"----compositions{int(time.time()*1000)}"
    fname = os.path.basename(path)
    mime = mimetypes.guess_type(fname)[0] or "image/jpeg"
    with open(path, "rb") as f:
        body = f.read()
    payload = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="upfile"; filename="{fname}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        "https://yandex.com/images/search?rpt=imageview&format=json"
        '&request=%7B%22blocks%22%3A%5B%7B%22block%22%3A%22b-page_type_search-by-image__link%22%7D%5D%7D',
        data=payload,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": _YANDEX_UA,
            "Accept": "*/*",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("blocks") or []
    if not blocks:
        raise RuntimeError("no blocks in upload response")
    cbir = blocks[0].get("params", {}).get("cbirId")
    if not cbir:
        raise RuntimeError("no cbirId in upload response")
    return cbir


def _yandex_fetch_text(query: str, page: int = 0) -> str:
    qs = urllib.parse.urlencode({"text": query, "p": page})
    url = f"https://yandex.com/images/search?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _YANDEX_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _yandex_fetch_results(cbir_id: str, page: int = 0) -> str:
    qs = urllib.parse.urlencode({
        "cbir_id": cbir_id, "rpt": "imageview", "p": page,
    })
    url = f"https://yandex.com/images/search?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _YANDEX_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


_YANDEX_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

_YANDEX_IMG_URL_RE = re.compile(
    r"https://(?!avatars\.mds\.yandex|yastatic|yandex\.com|yandex\.ru|mc\.yandex|favicon)"
    r"[a-zA-Z0-9._-]+\.[a-zA-Z]{2,5}/[a-zA-Z0-9_/?=&;.,%~+@-]+?"
    r"\.(?:jpg|jpeg|png|webp|gif)",
    re.IGNORECASE,
)


def _yandex_extract_image_urls(html: str) -> list[str]:
    """Pull direct third-party image URLs out of a Yandex SERP HTML page.

    Yandex stores result data in `data-bem` JSON attributes embedded in HTML;
    direct image URLs are also sprinkled throughout the HTML escape-encoded.
    A regex over the raw HTML text catches both.
    """
    # Decode HTML entities Yandex uses (&quot; etc.) so the regex can match cleanly
    decoded = html.replace("&quot;", '"').replace("&amp;", "&")
    seen = []
    seen_set = set()
    for m in _YANDEX_IMG_URL_RE.finditer(decoded):
        u = m.group(0)
        # Trim accidental trailing punctuation
        u = u.rstrip('.,;)"\'')
        if u in seen_set:
            continue
        seen_set.add(u)
        seen.append(u)
    return seen


def _upload_for_yandex(path: str, host: str = "catbox") -> str:
    """Upload a local file to a free public host so SerpApi can fetch it.

    Returns the public URL.
    Default: catbox.moe (no API key, persistent unless inactive).
    Alternative: 0x0.st (no API key, ~30-day expiry).
    """
    if host == "0x0":
        cmd = ["curl", "-sS", "-F", f"file=@{path}", "https://0x0.st"]
    else:
        cmd = [
            "curl", "-sS",
            "-F", "reqtype=fileupload",
            "-F", f"fileToUpload=@{path}",
            "https://catbox.moe/user/api.php",
        ]
    out = subprocess.check_output(cmd, timeout=60).decode().strip()
    if not (out.startswith("http://") or out.startswith("https://")):
        raise RuntimeError(f"unexpected upload response: {out[:200]}")
    return out


def fetch_lora_generate(params: dict, **_kw) -> Iterable[dict]:
    """Generate aesthetic-coherent images from a trained LoRA.

    Not implemented yet. Plan: call Replicate / Together / local mlx-stable-
    diffusion with the spec's lora_path + prompts, write to dest_root, yield
    candidates with source_type='lora'. Useful for long-tail aesthetic fields
    where no found-source has enough volume.
    """
    if False:
        yield
    return


def fetch_tiktok_hashtag(params: dict, **_kw) -> Iterable[dict]:
    """Pull TikTok videos by hashtag via yt-dlp; orchestrator can do keyframe
    extraction afterwards to get stills. Not implemented yet."""
    if False:
        yield
    return


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

ADAPTERS = {
    "reddit": fetch_reddit,
    "wikimedia": fetch_wikimedia,
    "gallery_dl": fetch_gallery_dl,
    "yandex_reverse": fetch_yandex_reverse,    # SerpApi-wrapped (paid, reliable)
    "yandex_direct": fetch_yandex_direct,      # No-API direct scrape (free)
    "pinterest_browse": fetch_pinterest_browse,  # Playwright browser scrape (free)
    "lora_generate": fetch_lora_generate,
    "tiktok_hashtag": fetch_tiktok_hashtag,
}


def fetch_source(source_type: str, params: dict, *, allow_video: bool = False):
    fn = ADAPTERS.get(source_type)
    if fn is None:
        raise ValueError(f"unknown source type: {source_type}")
    return fn(params, allow_video=allow_video) if source_type in ("reddit",) else fn(params)
