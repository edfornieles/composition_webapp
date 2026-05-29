"""Bucket spec — the unit of aesthetic-coherent image collection.

A bucket is a single aesthetic field (e.g. "goth_darkforest_landscapes") with a
target count of 1–2k images. The spec captures everything needed to build it
and to top it up later.

Layout:
    /Volumes/Oom/aggregation/got/{bucket_name}/
        .spec.json            ← this file (the bucket's identity)
        .manifest.jsonl       ← per-image provenance, one JSON line per image
        {uuid}.jpg ...        ← actual files

Re-running the orchestrator on a bucket folder reads the spec, reads the
manifest, fetches new candidates from the listed sources, dedups against
known hashes, and tops up to target_count.

This module is shared by ideate / build / update.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

GOT_ROOT_DEFAULT = Path("/Volumes/Oom/aggregation/got")
SPEC_FILENAME = ".spec.json"
MANIFEST_FILENAME = ".manifest.jsonl"


@dataclass
class SourceConfig:
    """One source within a bucket spec.

    `type` selects which adapter handles it. Adapter-specific fields live in
    `params`. Examples:

        SourceConfig(type="reddit", params={
            "subreddits": ["EarthPorn", "ForestPorn", "Liminalspace"],
            "min_score": 50, "limit_per_sub": 800,
            "since": "2018-01-01"
        })

        SourceConfig(type="wikimedia", params={
            "categories": ["Forests at night", "Dark forests"],
            "limit": 500
        })

        SourceConfig(type="gallery_dl", params={
            "urls": [
                "https://are.na/channel/dark-forest-x83",
                "https://flickr.com/groups/dark_forest"
            ],
            "limit_per_url": 500
        })

        SourceConfig(type="yandex_reverse", params={
            "seed_paths": ["/path/to/seed1.jpg"],
            "limit": 200
        })

        SourceConfig(type="lora_generate", params={
            "lora_path": "...", "prompts": ["dark forest mist"],
            "n": 500
        })
    """
    type: str
    params: dict = field(default_factory=dict)


@dataclass
class BucketSpec:
    name: str                                      # folder name; matches got/{name}/
    category: str                                  # broad category, e.g. "goth"
    subcategory: str = ""                          # narrowing, e.g. "darkforest_landscapes"
    target_count: int = 2000
    ruscha_rule: str = ""                          # the single-constraint aesthetic frame
    include_terms: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    seed_image_paths: list[str] = field(default_factory=list)   # absolute paths to exemplars
    sources: list[SourceConfig] = field(default_factory=list)
    min_resolution: int = 768                      # min long edge in px
    max_aspect_ratio: float = 3.0                  # reject extreme panoramas / strips
    allow_video: bool = False
    aesthetic_threshold: float = 0.0               # CLIP cosine min vs centroid (0 = off)
    notes: str = ""
    created_at: int = 0
    last_run_at: int = 0
    schema_version: int = 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = [asdict(s) if isinstance(s, SourceConfig) else s for s in self.sources]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BucketSpec":
        sources = [SourceConfig(**s) if not isinstance(s, SourceConfig) else s
                   for s in d.get("sources", [])]
        return cls(
            name=d["name"],
            category=d.get("category", ""),
            subcategory=d.get("subcategory", ""),
            target_count=int(d.get("target_count", 2000)),
            ruscha_rule=d.get("ruscha_rule", ""),
            include_terms=list(d.get("include_terms", [])),
            exclude_terms=list(d.get("exclude_terms", [])),
            seed_image_paths=list(d.get("seed_image_paths", [])),
            sources=sources,
            min_resolution=int(d.get("min_resolution", 768)),
            max_aspect_ratio=float(d.get("max_aspect_ratio", 3.0)),
            allow_video=bool(d.get("allow_video", False)),
            aesthetic_threshold=float(d.get("aesthetic_threshold", 0.0)),
            notes=d.get("notes", ""),
            created_at=int(d.get("created_at", 0)),
            last_run_at=int(d.get("last_run_at", 0)),
            schema_version=int(d.get("schema_version", 1)),
        )

    def bucket_dir(self, root: Path = GOT_ROOT_DEFAULT) -> Path:
        return root / self.name


# ---------------------------------------------------------------------------
# Spec I/O
# ---------------------------------------------------------------------------

def write_spec(spec: BucketSpec, root: Path = GOT_ROOT_DEFAULT) -> Path:
    bdir = spec.bucket_dir(root)
    bdir.mkdir(parents=True, exist_ok=True)
    if not spec.created_at:
        spec.created_at = int(time.time())
    path = bdir / SPEC_FILENAME
    path.write_text(json.dumps(spec.to_dict(), indent=2))
    return path


def read_spec(bucket_dir: Path) -> BucketSpec:
    path = Path(bucket_dir) / SPEC_FILENAME
    return BucketSpec.from_dict(json.loads(path.read_text()))


def has_spec(bucket_dir: Path) -> bool:
    return (Path(bucket_dir) / SPEC_FILENAME).exists()


# ---------------------------------------------------------------------------
# Manifest — per-image provenance
# ---------------------------------------------------------------------------

@dataclass
class ManifestRow:
    file_uuid: str           # uuid4 used as filename stem
    filename: str            # uuid + extension, e.g. "abc123.jpg"
    source_type: str         # reddit / wikimedia / gallery_dl / yandex / lora ...
    source_url: str
    source_subkey: str       # subreddit name / wikimedia category / are.na channel slug
    sha256: str
    phash: str = ""          # perceptual hash (hex)
    width: int = 0
    height: int = 0
    bytes: int = 0
    mime: str = ""
    fetched_at: int = 0
    aesthetic_score: float = 0.0   # cosine vs centroid (if gate enabled)
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def append_manifest(bucket_dir: Path, row: ManifestRow) -> None:
    bdir = Path(bucket_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    with (bdir / MANIFEST_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(row.to_json() + "\n")


def read_manifest(bucket_dir: Path):
    path = Path(bucket_dir) / MANIFEST_FILENAME
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def known_hashes(bucket_dir: Path) -> tuple[set[str], set[str]]:
    """Return (sha256_set, phash_set) of items already in the bucket."""
    sha = set()
    phs = set()
    for row in read_manifest(bucket_dir):
        if row.get("sha256"):
            sha.add(row["sha256"])
        if row.get("phash"):
            phs.add(row["phash"])
    return sha, phs


def manifest_count(bucket_dir: Path) -> int:
    return sum(1 for _ in read_manifest(bucket_dir))


# ---------------------------------------------------------------------------
# Helper: generate a file UUID stem for new images.
# ---------------------------------------------------------------------------

def new_uuid_stem() -> str:
    return uuid.uuid4().hex


if __name__ == "__main__":
    # Tiny smoke test: build a spec, write it, read it back.
    import tempfile
    spec = BucketSpec(
        name="test_bucket",
        category="test",
        target_count=10,
        ruscha_rule="testing",
        sources=[SourceConfig(type="reddit", params={"subreddits": ["EarthPorn"]})],
    )
    with tempfile.TemporaryDirectory() as td:
        write_spec(spec, root=Path(td))
        loaded = read_spec(Path(td) / "test_bucket")
        assert loaded.name == "test_bucket"
        assert loaded.sources[0].type == "reddit"
        assert loaded.sources[0].params["subreddits"] == ["EarthPorn"]
        print("ok")
