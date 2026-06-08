#!/usr/bin/env python3
"""Batch-generate the preview_10s clip for every composition that doesn't have one.

Usage (from project root):
    nohup ./batch_generate_previews.py [--concurrency N] [--kind KIND] </dev/null >> media_gen.log 2>&1 &
    disown

Or just:
    ./batch_generate_previews.py --concurrency 2

Behavior:
  • Finds every Composition with a URL but no `ready` preview_10s asset.
  • Spawns up to --concurrency Playwright subprocesses at a time (default 2).
  • Each subprocess writes START/DONE/FAIL lines to media_gen.log (same format
    as the interactive endpoints), so `tail -f media_gen.log` shows progress.
  • Pre-marks each asset record as `rendering` to avoid duplicate work if you
    also click regen in the UI while this is running.
  • Idempotent: re-running skips compositions that finished successfully and
    retries any that ended in `failed`.
  • Ctrl-C cleanly waits for in-flight jobs to finish before exiting.
  • To target a different kind: --kind poster|preview_10s|collector_45s

Why a separate process pool (not just N subprocess.Popen calls): we need a
bounded queue. Spawning 1600 Chromiums at once OOMs the machine. This script
holds the pool itself and never lets more than N captures run in parallel.
"""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

def setup_django():
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(str(PROJECT_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
    import django
    django.setup()


def find_missing(kind: str, *, require_kind_ready: str | None = None) -> list[int]:
    """Compositions with a URL that don't yet have a `ready` asset of `kind`.

    If `require_kind_ready` is given, the result is further filtered to only
    compositions that already have a `ready` asset of THAT kind. Useful for
    e.g. "generate GIFs only for compositions that already have a 45s
    collector" — pass kind="preview_gif", require_kind_ready="collector_45s".
    """
    from djangoscrap.models import Composition, CompositionMediaAsset
    has_ready = set(
        CompositionMediaAsset.objects
        .filter(kind=kind, status="ready")
        .values_list("composition_id", flat=True)
    )
    qs = (
        Composition.objects
        .exclude(url__isnull=True)
        .exclude(url__exact="")
        .exclude(id__in=has_ready)
    )
    if require_kind_ready:
        required = set(
            CompositionMediaAsset.objects
            .filter(kind=require_kind_ready, status="ready")
            .values_list("composition_id", flat=True)
        )
        qs = qs.filter(id__in=required)
    return list(qs.order_by("id").values_list("id", flat=True))


def premark_rendering(composition_id: int, kind: str):
    from djangoscrap.models import CompositionMediaAsset
    asset, _ = CompositionMediaAsset.objects.get_or_create(
        composition_id=composition_id, kind=kind
    )
    asset.status = "rendering"
    asset.error_message = ""
    asset.save(update_fields=["status", "error_message", "updated_at"])


def run_one(composition_id: int, kind: str) -> tuple[int, str, float]:
    """Spawn a single capture subprocess; return (id, status, elapsed_s)."""
    script = (
        "import os, sys, time, traceback, django\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        f"os.chdir({str(PROJECT_ROOT)!r})\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'djangoscrap.settings')\n"
        "django.setup()\n"
        "from djangoscrap.nft_media import generate_composition_media_assets\n"
        "from djangoscrap.models import Composition\n"
        f"_cid = {int(composition_id)}\n"
        f"_kind = {kind!r}\n"
        "_label = f'comp={_cid} kinds={_kind} force=True (BATCH)'\n"
        "_t = time.time()\n"
        "print(f'[{time.strftime(\"%H:%M:%S\")}] START {_label}', flush=True)\n"
        "try:\n"
        "    c = Composition.objects.prefetch_related('media_assets').get(id=_cid)\n"
        "    res = generate_composition_media_assets(c, force=True, kinds=[_kind])\n"
        "    dt = time.time() - _t\n"
        "    written = list(res.keys()) if res else []\n"
        "    print(f'[{time.strftime(\"%H:%M:%S\")}] DONE  {_label} in {dt:.1f}s -> {written}', flush=True)\n"
        "    sys.exit(0)\n"
        "except Exception as e:\n"
        "    dt = time.time() - _t\n"
        "    print(f'[{time.strftime(\"%H:%M:%S\")}] FAIL  {_label} after {dt:.1f}s: {type(e).__name__}: {e}', flush=True)\n"
        "    traceback.print_exc()\n"
        "    sys.exit(1)\n"
    )

    log_path = PROJECT_ROOT / "media_gen.log"
    t0 = time.time()
    with open(log_path, "ab") as logf:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            close_fds=True,
        )
    return composition_id, "ok" if proc.returncode == 0 else "fail", time.time() - t0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--concurrency", type=int, default=2, help="parallel captures (default 2)")
    parser.add_argument("--kind", default="preview_10s",
                        choices=["poster", "preview_10s", "collector_45s", "preview_gif"],
                        help="which media kind to generate (default preview_10s)")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N compositions (0 = all)")
    parser.add_argument("--require-kind-ready", default=None,
                        choices=["poster", "preview_10s", "collector_45s", "preview_gif"],
                        help="restrict to compositions that already have this OTHER kind ready "
                             "(e.g. --kind preview_gif --require-kind-ready collector_45s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="just print what would be done")
    args = parser.parse_args()

    setup_django()
    missing = find_missing(args.kind, require_kind_ready=args.require_kind_ready)
    if args.limit:
        missing = missing[:args.limit]

    print(f"[batch] target kind = {args.kind}")
    print(f"[batch] missing     = {len(missing)} compositions")
    print(f"[batch] concurrency = {args.concurrency}")
    print(f"[batch] log file    = {PROJECT_ROOT / 'media_gen.log'}")
    if args.dry_run:
        print(f"[batch] dry-run; first 20 ids: {missing[:20]}")
        return

    if not missing:
        print("[batch] nothing to do — exiting")
        return

    print(f"[batch] starting at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    done = 0
    failed = 0
    t0 = time.time()
    # Pre-mark them all rendering so any UI viewer sees the queue.
    for cid in missing:
        premark_rendering(cid, args.kind)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_one, cid, args.kind): cid for cid in missing}
        try:
            for f in as_completed(futures):
                cid = futures[f]
                try:
                    _, status, elapsed = f.result()
                except Exception as e:
                    print(f"[batch] EXC comp={cid}: {type(e).__name__}: {e}")
                    failed += 1
                    continue
                if status == "ok":
                    done += 1
                else:
                    failed += 1
                # Light heartbeat to stderr so a tail -f shows overall progress
                total = done + failed
                elapsed_total = time.time() - t0
                rate = total / elapsed_total if elapsed_total > 0 else 0
                eta = (len(missing) - total) / rate if rate > 0 else 0
                print(
                    f"[batch] {total}/{len(missing)}  ok={done} fail={failed}  "
                    f"last comp={cid}({status}, {elapsed:.0f}s)  "
                    f"rate={rate*60:.1f}/min  eta={eta/3600:.1f}h",
                    flush=True,
                )
        except KeyboardInterrupt:
            print("[batch] Ctrl-C received; waiting for in-flight jobs to finish...")
            for f in futures:
                if not f.done():
                    f.cancel()

    print(f"[batch] finished at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[batch] total: {done} ok, {failed} fail in {(time.time()-t0)/3600:.2f}h")


if __name__ == "__main__":
    main()
