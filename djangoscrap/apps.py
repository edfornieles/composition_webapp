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
        # The thread is a no-op if sentence-transformers is not installed.
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
