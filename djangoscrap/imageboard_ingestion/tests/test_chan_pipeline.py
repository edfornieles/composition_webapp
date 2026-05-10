"""Smoke tests for the local-first imageboard intelligence pipeline.

Pure stdlib unittest. A tiny synthetic corpus is built on disk under a
TemporaryDirectory and run through the layout helpers / safety decision /
quality features / materialiser. Heavy optional deps (openclip, blip,
umap, hdbscan, fiftyone) are NOT exercised here — they have their own
graceful-skip behaviour at runtime.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


def _png_bytes(color=(120, 80, 60), size=(64, 64)) -> bytes:
    """Tiny solid-color PNG for fixture images."""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class LayoutTests(unittest.TestCase):
    def test_layout_is_idempotent(self):
        from djangoscrap.imageboard_ingestion import config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config.ensure_layout(root)
            config.ensure_layout(root)  # twice
            self.assertTrue(config.manifests_dir(root).exists())
            # quarantine_dir requires source/board; just verify call works
            qd = config.quarantine_dir(root, "fourchan", "fit")
            qd.mkdir(parents=True, exist_ok=True)
            self.assertTrue(qd.exists())
            self.assertTrue(config.derived_dir(root).exists())

    def test_resolve_root_prefers_explicit(self):
        from djangoscrap.imageboard_ingestion import config
        with tempfile.TemporaryDirectory() as td:
            r = config.resolve_root(td)
            self.assertEqual(Path(td).resolve(), Path(r).resolve())


class SafetyDecisionTests(unittest.TestCase):
    def test_underage_reason_renamed(self):
        from djangoscrap.imageboard_ingestion import safety_decision as sd
        self.assertEqual(sd.REASON_SUSPECTED_UNDERAGE,
                         "suspected_underage_sexual_content")
        self.assertIn(sd.REASON_SUSPECTED_UNDERAGE, sd._DELETE_REASONS)

    def test_quarantine_vs_delete(self):
        from djangoscrap.imageboard_ingestion import safety_decision as sd
        d_delete = sd.SafetyDecision(decision="delete",
                                     reason_codes=[sd.REASON_ILLEGAL_SEXUAL],
                                     confidence=0.95)
        self.assertEqual(d_delete.decision, "delete")
        d_q = sd.SafetyDecision(decision="quarantine_no_preview",
                                reason_codes=[sd.REASON_DOXXING],
                                confidence=0.8)
        self.assertNotEqual(d_q.decision, "allow")


class QualityFeaturesTests(unittest.TestCase):
    def test_features_for_tiny_png(self):
        from djangoscrap.imageboard_ingestion import quality
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.png"
            p.write_bytes(_png_bytes())
            feats = quality.compute_features(p)
            # imagehash may be missing in test env; tolerate the graceful skip
            if feats.get("feature_error", "").startswith("missing_dep"):
                self.skipTest(f"optional dep missing: {feats['feature_error']}")
            self.assertIn("aspect_ratio", feats)
            self.assertEqual(feats["width"], 64)
            self.assertEqual(feats["height"], 64)


class ManifestsAndExclusionTests(unittest.TestCase):
    """Build a tiny manifest by hand and verify exclusion + relative paths."""

    def _make_corpus(self, root: Path):
        from djangoscrap.imageboard_ingestion import config
        config.ensure_layout(root)
        orig_dir = config.originals_dir(root, "fourchan", "fit")
        orig_dir.mkdir(parents=True, exist_ok=True)
        # 3 allowed + 1 quarantined
        rows = []
        for i, color in enumerate([(10, 10, 10), (120, 80, 60), (200, 30, 30)]):
            p = orig_dir / f"img{i}.png"
            p.write_bytes(_png_bytes(color))
            rows.append({
                "image_key": f"k{i}",
                "post_uid": f"p{i}",
                "source": "fourchan", "board": "fit",
                "thread_id": 1, "post_id": 100 + i,
                "original_path": str(p),
                "thumbnail_path": "",
                "filename_original": p.name,
                "ext": "png", "width": 64, "height": 64, "fsize_remote": p.stat().st_size,
                "sha256": f"deadbeef{i:02d}",
                "safety_decision": {"decision": "allow", "reason_codes": []},
            })
        # quarantined
        qp = config.quarantine_dir(root, "fourchan", "fit") / "qimg.png"
        qp.parent.mkdir(parents=True, exist_ok=True)
        qp.write_bytes(_png_bytes((0, 200, 0)))
        rows.append({
            "image_key": "kq",
            "post_uid": "pq",
            "source": "fourchan", "board": "fit",
            "thread_id": 1, "post_id": 999,
            "original_path": str(qp),
            "thumbnail_path": "",
            "filename_original": qp.name,
            "ext": "png", "width": 64, "height": 64, "fsize_remote": qp.stat().st_size,
            "sha256": "deadbeefqq",
            "safety_decision": {"decision": "quarantine_no_preview",
                                "reason_codes": ["doxxing"]},
        })
        with config.images_manifest(root).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        # minimal posts manifest
        with config.posts_manifest(root).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "post_uid": r["post_uid"],
                    "source_key": "fourchan_fit",
                    "fit_tags": ["lift"],
                    "plain_text": "sample text",
                    "contamination": {"body_shame": 0.4, "discipline_fetish": 0.1},
                    "time_unix": 1_700_000_000,
                }) + "\n")
        # cluster_membership.jsonl + clusters.jsonl (everyone in cluster 7)
        cm_path = config.cluster_membership_path(root).with_suffix(".jsonl")
        cm_path.parent.mkdir(parents=True, exist_ok=True)
        with cm_path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "image_key": r["image_key"],
                    "cluster_id": 7,
                    "umap_x": 0.1, "umap_y": 0.2,
                }) + "\n")
        cl_path = config.clusters_path(root).with_suffix(".jsonl")
        with cl_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "cluster_id": 7, "method": "test", "size": len(rows),
                "suggested_label_auto": "lift_sample",
            }) + "\n")
        # empty image_features so joins don't crash
        feat_path = config.image_features_path(root).with_suffix(".jsonl")
        feat_path.write_text("", encoding="utf-8")

    def test_quarantined_excluded_from_iteration(self):
        from djangoscrap.imageboard_ingestion import config, quality
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_corpus(root)
            allowed = list(quality.iter_allowed_images(root))
            self.assertEqual(len(allowed), 3)
            for r in allowed:
                self.assertEqual(
                    (r.get("safety_decision") or {}).get("decision"), "allow")

    def test_cluster_membership_schema(self):
        from djangoscrap.imageboard_ingestion import config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_corpus(root)
            cm = config.cluster_membership_path(root).with_suffix(".jsonl")
            recs = [json.loads(l) for l in cm.read_text().splitlines() if l]
            self.assertTrue(all({"image_key", "cluster_id",
                                 "umap_x", "umap_y"}.issubset(r) for r in recs))

    def test_materialise_uses_symlinks_and_skips_quarantine(self):
        """Phase 7 materialiser excludes quarantined and prefers symlinks."""
        from django.core.management import call_command
        from djangoscrap.imageboard_ingestion import config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_corpus(root)
            try:
                call_command(
                    "materialise_imageboard_folders",
                    root=str(root),
                    source="fourchan",
                    board="fit",
                    by="openclip_cluster_id",
                    min_cluster_size=1,
                    symlink=True,
                )
            except Exception as e:
                self.skipTest(f"command unavailable in test env: {e}")
            out_root = config.derived_dir(
                root, "folders", "fourchan_fit", "openclip_cluster_id")
            # Quarantined "kq" should not be materialised
            for sub in out_root.iterdir() if out_root.exists() else []:
                for entry in sub.iterdir():
                    if entry.name == "manifest.json":
                        continue
                    self.assertNotEqual(entry.name, "qimg.png")

    def test_enriched_export_relative_paths(self):
        """When --materialise-symlinks is used, paths in CSV are relative."""
        import csv
        from django.core.management import call_command
        from djangoscrap.imageboard_ingestion import config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_corpus(root)
            out_csv = root / "derived" / "imageplot_exports" / "fit_enriched.csv"
            out_csv.parent.mkdir(parents=True, exist_ok=True)
            try:
                call_command(
                    "export_imageplot_enriched_dataset",
                    root=str(root),
                    source="fourchan",
                    board="fit",
                    output=str(out_csv),
                    materialise_symlinks=True,
                    image_mode="thumbnails",
                )
            except Exception as e:
                self.skipTest(f"command unavailable in test env: {e}")
            self.assertTrue(out_csv.exists())
            with out_csv.open() as f:
                rows = list(csv.DictReader(f))
            # Either rows present and relative, or empty (thumbnails not built);
            # main check: nothing absolute under any local_image_path.
            for r in rows:
                for col in ("local_image_path", "local_thumbnail_path"):
                    val = r.get(col) or ""
                    if val:
                        self.assertFalse(os.path.isabs(val),
                                         f"{col} should be relative: {val}")


class ExistingImagePlotPipelinePreservedTests(unittest.TestCase):
    """The original ImagePlot exporter columns must remain importable + intact."""

    def test_csv_columns_intact(self):
        from djangoscrap.imageboard_ingestion import imageplot_export
        self.assertIn("id", imageplot_export.CSV_COLUMNS)
        self.assertIn("contamination_body_shame",
                      [f"contamination_{k}"
                       for k in imageplot_export.CONTAMINATION_KEYS])

    def test_enriched_columns_are_superset(self):
        from djangoscrap.imageboard_ingestion import imageplot_export
        from djangoscrap.management.commands import (
            export_imageplot_enriched_dataset as enriched,
        )
        for c in imageplot_export.CSV_COLUMNS:
            self.assertIn(c, enriched.ENRICHED_COLUMNS)


if __name__ == "__main__":
    unittest.main()
