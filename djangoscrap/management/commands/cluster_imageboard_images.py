"""Cluster the local chan-corpus image embeddings into emergent categories.

    python manage.py cluster_imageboard_images \\
        --root /Volumes/oom/chan_corpus \\
        --source fourchan --board fit \\
        --method hdbscan --umap
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import clustering, config


class Command(BaseCommand):
    help = "UMAP + HDBSCAN/KMeans clustering of OpenCLIP embeddings."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=None)
        parser.add_argument("--source", default="fourchan")
        parser.add_argument("--board", required=True)
        parser.add_argument("--model-tag", default="ViT-B-32_laion2b_s34b_b79k",
                            help="Must match the tag used by embed_imageboard_images.")
        parser.add_argument("--method", choices=["hdbscan", "kmeans"], default="hdbscan")
        parser.add_argument("--n-clusters", type=int, default=50,
                            help="KMeans only.")
        parser.add_argument("--umap", action="store_true", default=True,
                            help="Use UMAP for cluster space (default).")
        parser.add_argument("--no-umap", dest="umap", action="store_false")
        parser.add_argument("--umap-n-components", type=int, default=2)
        parser.add_argument("--umap-n-neighbors", type=int, default=15)
        parser.add_argument("--umap-min-dist", type=float, default=0.1)
        parser.add_argument("--min-cluster-size", type=int, default=25,
                            help="HDBSCAN only.")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root)
        try:
            stats = clustering.cluster(
                root=root,
                source=opts["source"],
                board=opts["board"],
                model_tag=opts["model_tag"],
                method=opts["method"],
                n_clusters=int(opts["n_clusters"]),
                use_umap=bool(opts["umap"]),
                umap_n_components=int(opts["umap_n_components"]),
                umap_n_neighbors=int(opts["umap_n_neighbors"]),
                umap_min_dist=float(opts["umap_min_dist"]),
                min_cluster_size=int(opts["min_cluster_size"]),
                log=self.stdout.write,
            )
        except FileNotFoundError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"clustering done: {stats}"))
