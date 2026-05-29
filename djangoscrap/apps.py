import os
import threading
import logging
from django.apps import AppConfig

logger = logging.getLogger("djangoscrap.clip")


class DjangoscrapConfig(AppConfig):
    name = "djangoscrap"
    verbose_name = "Djangoscrap"

    def ready(self):
        # Warm the CLIP model in a daemon thread so it's ready before the first
        # ingestion task runs without blocking server / worker startup.
        #
        # OFF BY DEFAULT in the web server context: loading CLIP ViT-B/32
        # (~600 MB / 398 weight tensors) right after gunicorn forks a worker
        # pushes the worker's resident size over the macOS jetsam threshold
        # under any concurrent web load — the worker gets SIGKILL'd before it
        # can serve a single request, and gunicorn spawns another, ad infinitum.
        # Lazy loading via _clip_get_model still works on first ingestion call.
        # Set DJANGOSCRAP_WARM_CLIP=1 to opt back in (e.g. for ingestion workers).
        if os.environ.get("DJANGOSCRAP_WARM_CLIP", "").strip().lower() in ("1", "true", "yes", "on"):
            t = threading.Thread(target=_warm_clip, daemon=True, name="clip-warmup")
            t.start()


def _warm_clip():
    try:
        from djangoscrap.views._ingestion_dedup import _clip_get_model
        model = _clip_get_model()
        if model is not None:
            logger.info("CLIP model warm-up complete (%s)", model)
    except Exception as exc:
        logger.debug("CLIP warm-up skipped: %s", exc)
