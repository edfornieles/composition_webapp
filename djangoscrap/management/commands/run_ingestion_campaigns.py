import time
import signal

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from djangoscrap.models import IngestionBatch
from djangoscrap.views import _run_campaign_cycle


class Command(BaseCommand):
    help = "Run due autonomous ingestion campaigns."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10, help="Max due batches to process per tick.")
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Keep running forever, ticking on a fixed interval (Ctrl+C to stop).",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=300,
            help="Seconds between ticks when --loop is set (default: 300).",
        )

    def _tick_once(self, limit: int) -> int:
        now_ts = timezone.now()
        due = (
            IngestionBatch.objects.filter(campaign_enabled=True)
            .filter(campaign_next_run_at__lte=now_ts)
            .order_by("campaign_next_run_at")[:limit]
        )
        ran = 0
        for batch in due:
            try:
                result = _run_campaign_cycle(batch, trigger="scheduled")
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"[batch={batch.id}] ERROR :: {exc}")
                continue
            ran += 1
            self.stdout.write(
                f"[batch={batch.id}] {result.get('status')} :: {result.get('message')}"
            )
        return ran

    def handle(self, *args, **options):
        limit = max(1, min(100, int(options.get("limit") or 10)))
        loop = bool(options.get("loop"))
        interval = max(30, int(options.get("interval") or 300))

        if not loop:
            ran = self._tick_once(limit)
            if not ran:
                self.stdout.write("No due campaigns.")
            else:
                self.stdout.write(self.style.SUCCESS(f"Processed {ran} campaign batch(es)."))
            return

        stop = {"flag": False}

        def _stop_handler(signum, frame):  # noqa: ARG001
            stop["flag"] = True
            self.stdout.write(self.style.WARNING("\nStopping after current tick..."))

        signal.signal(signal.SIGINT, _stop_handler)
        signal.signal(signal.SIGTERM, _stop_handler)

        self.stdout.write(
            self.style.SUCCESS(
                f"Loop mode: tick every {interval}s, up to {limit} batches per tick. Ctrl+C to stop."
            )
        )
        while not stop["flag"]:
            tick_started = timezone.now()
            try:
                ran = self._tick_once(limit)
                self.stdout.write(
                    f"[{tick_started.strftime('%Y-%m-%d %H:%M:%S')}] tick: {ran} batch(es) processed."
                )
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"[{tick_started.isoformat()}] tick failed: {exc}")
            finally:
                close_old_connections()
            for _ in range(interval):
                if stop["flag"]:
                    break
                time.sleep(1)
        self.stdout.write(self.style.SUCCESS("Loop stopped."))
