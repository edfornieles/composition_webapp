import io
import base64
import json
import math
import random
import re
import csv
import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, unquote
from urllib.request import Request, urlopen

from django.conf import settings
from PIL import Image


USER_AGENT = "Mozilla/5.0 (AssociationsComposer/1.0)"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.engineering/v1/images/"
FLICKR_PUBLIC_API = "https://www.flickr.com/services/feeds/photos_public.gne"
OPENAI_EMBED_MODEL = "text-embedding-3-small"
OPENAI_CHAT_MODEL = "gpt-4o-mini"
_CLIP_MODEL_SINGLETON = None
_TASTE_MODEL_CACHE: dict[str, Any] | None = None
_TASTE_MODEL_PATH = Path(__file__).resolve().parent / "data" / "associations_taste_model.json"
_FEEDBACK_MODEL_CACHE: dict[str, Any] | None = None
_FEEDBACK_MODEL_PATH = Path(__file__).resolve().parent / "data" / "associations_feedback_model.json"
_FEEDBACK_LOG_PATH = Path(__file__).resolve().parent / "data" / "associations_feedback.jsonl"
_TRAINING_CAPTIONS_CACHE: dict[str, dict[str, str]] | None = None
_TEXT_HASH_DIM = 256
_IMAGE_WEIGHT_DEFAULT = 0.72
_SOURCE_TEXT_WEIGHT_DEFAULT = 0.7
_PERSONAL_TEXT_WEIGHT_DEFAULT = 0.3
_ASSOCIATIONS_TRAINING_SET_DIR = (Path(__file__).resolve().parent.parent / "associations" / "associations training set").resolve()
_LOCAL_SOURCES_ROOT = (Path(settings.BASE_DIR).parent / "composition_sources_unprocessed").resolve()
_ASSOCIATIONS_CHAINS_ROOT = settings.ASSOCIATIONS_CHAINS_ROOT.resolve()
_PATTERN_FORM_TERMS = {
    "house", "home", "building", "architecture", "roof", "window", "door",
    "bunker", "shelter", "cave", "tunnel", "underground", "subterranean", "basement", "cellar",
    "grid", "line", "stripe", "circle", "ring", "spiral", "curve", "wave", "mesh", "network",
    "surface", "texture", "pattern", "layer", "fragment", "frame", "portal", "void",
}


def _host_resolves_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


@dataclass
class Candidate:
    url: str
    title: str
    source: str = "web"


@dataclass
class CandidateFeatures:
    candidate: Candidate
    hist_vec: list[float] | None
    hash_bits: str
    clip_vec: list[float] | None


def _safe_json_loads(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


def _dot(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum((x * y) for x, y in zip(a, b)))


def _norm(v: list[float]) -> float:
    return math.sqrt(sum((x * x) for x in v)) or 1.0


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return _dot(a, b) / (_norm(a) * _norm(b))


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _vec_sub(a: list[float], b: list[float]) -> list[float]:
    if not a or not b or len(a) != len(b):
        return []
    return [float(x - y) for x, y in zip(a, b)]


def _vec_add(a: list[float], b: list[float]) -> list[float]:
    if not a:
        return list(b)
    if not b:
        return list(a)
    if len(a) != len(b):
        return []
    return [float(x + y) for x, y in zip(a, b)]


def _vec_scale(v: list[float], s: float) -> list[float]:
    if not v:
        return []
    return [float(x * s) for x in v]


def _vec_normalize(v: list[float]) -> list[float]:
    if not v:
        return []
    n = _norm(v)
    if n <= 0:
        return []
    return [float(x / n) for x in v]


def _url_text_signature(url: str) -> str:
    raw = str(url or "").strip().lower()
    # Avoid leaking slug/source-folder names (e.g. "associations-mickey") into
    # semantic query text; these are routing artifacts, not image meaning.
    if "/source-media/associations-" in raw and "/assoc_" in raw:
        return ""
    parsed = raw
    parsed = re.sub(r"https?://", "", parsed)
    parsed = re.sub(r"[^a-z0-9/._-]+", " ", parsed)
    parsed = parsed.replace("/", " ")
    parsed = parsed.replace("_", " ")
    parsed = parsed.replace("-", " ")
    return re.sub(r"\s+", " ", parsed).strip()


def _local_file_from_url(url: str) -> Path | None:
    value = str(url or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    path = unquote(parsed.path or value)
    if not path:
        return None
    if path.startswith("/training-set-media/"):
        file_name = path.rsplit("/", 1)[-1].strip()
        if not file_name:
            return None
        p = (_ASSOCIATIONS_TRAINING_SET_DIR / file_name).resolve()
        if str(p).startswith(str(_ASSOCIATIONS_TRAINING_SET_DIR)) and p.exists() and p.is_file():
            return p
    if path.startswith("/association-chain-media/"):
        try:
            chain_enc, file_enc = path[len("/association-chain-media/"):].split("/", 1)
        except ValueError:
            return None
        p = (_ASSOCIATIONS_CHAINS_ROOT / unquote(chain_enc) / unquote(file_enc)).resolve()
        if _ASSOCIATIONS_CHAINS_ROOT in p.parents and p.exists() and p.is_file():
            return p
    if path.startswith("/source-media/"):
        try:
            source_enc, file_enc = path[len("/source-media/"):].split("/", 1)
        except ValueError:
            return None
        p = (_LOCAL_SOURCES_ROOT / unquote(source_enc) / unquote(file_enc)).resolve()
        if _LOCAL_SOURCES_ROOT in p.parents and p.exists() and p.is_file():
            return p
    return None


def _training_caption_for_url(url: str) -> str:
    global _TRAINING_CAPTIONS_CACHE
    local = _local_file_from_url(url)
    if not local:
        return ""
    if not str(local).startswith(str(_ASSOCIATIONS_TRAINING_SET_DIR)):
        return ""
    if _TRAINING_CAPTIONS_CACHE is None:
        try:
            _TRAINING_CAPTIONS_CACHE = _load_training_captions(_ASSOCIATIONS_TRAINING_SET_DIR)
        except Exception:
            _TRAINING_CAPTIONS_CACHE = {}
    cap = (_TRAINING_CAPTIONS_CACHE or {}).get(local.name) or {}
    source_text = str(cap.get("source_text") or "").strip()
    personal_text = str(cap.get("personal_text") or "").strip()
    return " ".join(x for x in [source_text, personal_text] if x).strip()


def _fetch_bytes(url: str, timeout: int = 10) -> bytes | None:
    local_path = _local_file_from_url(url)
    if local_path:
        try:
            return local_path.read_bytes()
        except Exception:
            return None
    value = str(url or "").strip()
    if value.startswith("data:image/") and ";base64," in value:
        try:
            encoded = value.split(";base64,", 1)[1]
            return base64.b64decode(encoded, validate=False)
        except Exception:
            return None
    try:
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and _host_resolves_private(parsed.hostname):
            return None
        req = Request(value, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _image_visual_vector_from_bytes(raw: bytes) -> list[float] | None:
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB").resize((32, 32), Image.LANCZOS)
    except Exception:
        return None
    px = list(img.getdata())
    if not px:
        return None
    bins = [0.0] * 24
    for r, g, b in px:
        rv = min(7, int((r / 255.0) * 8))
        gv = min(7, int((g / 255.0) * 8))
        bv = min(7, int((b / 255.0) * 8))
        bins[rv] += 1.0
        bins[8 + gv] += 1.0
        bins[16 + bv] += 1.0
    total = float(len(px))
    return [v / total for v in bins]


def _image_hash64_from_bytes(raw: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(raw)).convert("L").resize((8, 8), Image.LANCZOS)
        vals = list(img.getdata())
        if not vals:
            return ""
        avg = sum(vals) / len(vals)
        return "".join("1" if v >= avg else "0" for v in vals)
    except Exception:
        return ""


def _hamming_bits(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 999
    return sum(1 for x, y in zip(a, b) if x != y)


def _image_size_from_bytes(raw: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(raw)) as img:
            w, h = img.size
            return int(w or 0), int(h or 0)
    except Exception:
        return 0, 0


def _openai_client(api_key: str):
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def _get_clip_model():
    global _CLIP_MODEL_SINGLETON
    if _CLIP_MODEL_SINGLETON is not None:
        return _CLIP_MODEL_SINGLETON
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore

        _CLIP_MODEL_SINGLETON = SentenceTransformer("clip-ViT-B-32")
        return _CLIP_MODEL_SINGLETON
    except Exception:
        return None


def _clip_embed_image_bytes(raw: bytes) -> list[float] | None:
    model = _get_clip_model()
    if model is None:
        return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        vec = model.encode([img], normalize_embeddings=True)[0]
        return [float(x) for x in vec]
    except Exception:
        return None


def _image_embedding_from_bytes(raw: bytes) -> tuple[list[float] | None, str]:
    clip = _clip_embed_image_bytes(raw)
    if clip:
        return clip, "clip"
    hist = _image_visual_vector_from_bytes(raw)
    if hist:
        return hist, "hist"
    return None, ""


def _sorted_training_images(training_dir: Path) -> list[Path]:
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
    files = []
    for p in training_dir.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name.startswith("._"):
            continue
        if p.suffix.lower() not in image_exts:
            continue
        files.append(p)

    def sort_key(path: Path):
        m = re.search(r"(\d+)", path.stem)
        return (0, int(m.group(1))) if m else (1, path.name.lower())

    files.sort(key=sort_key)
    return files


def _tokenize_text(raw: str) -> list[str]:
    text = (raw or "").lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return [t for t in text.split() if t]


def _significant_terms(raw: str, max_terms: int = 12) -> list[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "image", "images", "photo",
        "jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "avif", "http", "https",
        "www", "com", "org", "net", "thumb", "upload", "media", "source", "training", "set",
        "visual", "culture",
    }
    terms: list[str] = []
    for tok in _tokenize_text(raw):
        if len(tok) < 3:
            continue
        if tok in stop:
            continue
        if tok.isdigit():
            continue
        if tok not in terms:
            terms.append(tok)
        if len(terms) >= max_terms:
            break
    return terms


def _anchor_terms_from_seed(seed_title: str, current_title: str, query: str) -> list[str]:
    combined = " ".join([seed_title or "", current_title or "", query or ""]).strip()
    return _significant_terms(combined, max_terms=14)


def _token_overlap_score(query_terms: list[str], candidate_text: str) -> float:
    if not query_terms:
        return 0.0
    cand_tokens = set(_tokenize_text(candidate_text))
    if not cand_tokens:
        return 0.0
    hits = sum(1 for t in query_terms if t in cand_tokens)
    return _clamp01(hits / max(1, min(len(query_terms), 6)))


def _hist_color_terms(hist: list[float] | None) -> list[str]:
    if not hist or len(hist) < 24:
        return []
    r_bins = hist[:8]
    g_bins = hist[8:16]
    b_bins = hist[16:24]

    def mean_channel(bins: list[float]) -> float:
        total = sum(float(x) for x in bins)
        if total <= 0:
            return 0.0
        return sum(((idx + 0.5) / 8.0) * float(v) for idx, v in enumerate(bins)) / total

    r = mean_channel(r_bins)
    g = mean_channel(g_bins)
    b = mean_channel(b_bins)
    out: list[str] = []
    brightness = (r + g + b) / 3.0
    if brightness < 0.3:
        out.append("dark")
    elif brightness > 0.72:
        out.append("bright")
    if abs(r - g) < 0.08 and abs(g - b) < 0.08:
        out.append("monochrome")
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx - mn < 0.1:
        out.append("muted")
    if r > 0.55 and g > 0.45 and b < 0.38:
        out.append("yellow")
    if r > 0.52 and b > 0.46 and g < 0.4:
        out.append("magenta")
    if g > 0.5 and b > 0.48 and r < 0.4:
        out.append("cyan")
    if r >= g and r >= b and r > 0.47:
        out.append("red")
    if g >= r and g >= b and g > 0.47:
        out.append("green")
    if b >= r and b >= g and b > 0.47:
        out.append("blue")
    return out[:4]


def _pattern_focus_terms(
    seed_title: str,
    current_title: str,
    query: str,
    concepts: list[str],
    visual_context: str,
    current_hist: list[float] | None,
) -> tuple[list[str], list[str]]:
    combined = " ".join(
        [
            seed_title or "",
            current_title or "",
            query or "",
            visual_context or "",
            " ".join(concepts[:6]),
        ]
    ).strip()
    sig = _significant_terms(combined, max_terms=22)
    forms = [t for t in sig if t in _PATTERN_FORM_TERMS]
    colors = _hist_color_terms(current_hist)
    focus = []
    for tok in forms + sig + colors:
        if tok not in focus:
            focus.append(tok)
        if len(focus) >= 14:
            break
    return focus, colors


def _color_resonance_score(current_hist: list[float] | None, candidate_hist: list[float] | None) -> float:
    cur_terms = set(_hist_color_terms(current_hist))
    cand_terms = set(_hist_color_terms(candidate_hist))
    if not cur_terms or not cand_terms:
        return 0.0
    inter = len(cur_terms & cand_terms)
    union = len(cur_terms | cand_terms)
    return _clamp01(inter / max(1, union))


def _seed_subject_terms(seed_title: str, current_title: str, visual_context: str, seed_caption: str, current_caption: str) -> list[str]:
    primary = _significant_terms(visual_context or "", max_terms=14)
    secondary = _significant_terms(" ".join([seed_title, current_title, seed_caption, current_caption]).strip(), max_terms=16)
    generic = {
        "visual", "culture", "image", "photo", "pattern", "form", "shape", "concept",
        "metaphor", "color", "atmosphere", "focus", "option", "novelty", "relation",
        "style", "mood", "first", "found", "titled", "using", "getty", "march",
        "april", "january", "february", "woman", "man", "people",
    }
    out: list[str] = []
    for tok in primary + secondary:
        if tok in generic:
            continue
        if tok not in out:
            out.append(tok)
        if len(out) >= 10:
            break
    return out


def _anchor_relevance_score(query_terms: list[str], candidate_text: str, text_score: float, llm_score: float) -> float:
    overlap = _token_overlap_score(query_terms, candidate_text)
    semantic = _clamp01((float(text_score) * 0.58) + (float(llm_score) * 0.42))
    if overlap > 0.0:
        return _clamp01((overlap * 0.7) + (semantic * 0.3))
    return _clamp01(semantic * 0.45)


def _pattern_relevance_score(
    pattern_terms: list[str],
    candidate_text: str,
    current_hist: list[float] | None,
    candidate_hist: list[float] | None,
    text_score: float,
    llm_score: float,
) -> tuple[float, float]:
    pattern_overlap = _token_overlap_score(pattern_terms[:12], candidate_text)
    color_resonance = _color_resonance_score(current_hist, candidate_hist)
    semantic = _clamp01((float(text_score) * 0.52) + (float(llm_score) * 0.48))
    pattern_focus = _clamp01((pattern_overlap * 0.58) + (color_resonance * 0.24) + (semantic * 0.18))
    return pattern_focus, color_resonance


def _text_hash_vector(text: str, dim: int = _TEXT_HASH_DIM) -> list[float]:
    tokens = _tokenize_text(text)
    if not tokens:
        return [0.0] * dim
    vec = [0.0] * dim
    for tok in tokens:
        idx = hash(tok) % dim
        vec[idx] += 1.0
    return _vec_normalize(vec)


def _load_training_captions(training_dir: Path) -> dict[str, dict[str, str]]:
    captions: dict[str, dict[str, str]] = {}
    csv_path = training_dir / "captions.csv"
    json_path = training_dir / "captions.json"
    txt_path = training_dir / "captions.txt"

    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames and {"filename", "text"}.issubset({x.strip().lower() for x in reader.fieldnames if x}):
                    for row in reader:
                        fn = str((row.get("filename") or row.get("file") or "")).strip()
                        tx = str((row.get("text") or row.get("caption") or "")).strip()
                        if fn and tx:
                            captions[Path(fn).name] = {"source_text": tx, "personal_text": "", "text": tx}
                elif reader.fieldnames and {"filename", "source_text", "personal_text"}.issubset({x.strip().lower() for x in reader.fieldnames if x}):
                    for row in reader:
                        fn = str((row.get("filename") or row.get("file") or "")).strip()
                        src = str((row.get("source_text") or "")).strip()
                        per = str((row.get("personal_text") or "")).strip()
                        if fn and (src or per):
                            captions[Path(fn).name] = {"source_text": src, "personal_text": per, "text": " ".join([src, per]).strip()}
                else:
                    f.seek(0)
                    for row in csv.reader(f):
                        if len(row) < 2:
                            continue
                        fn = str(row[0]).strip()
                        tx = ",".join(row[1:]).strip()
                        if fn and tx:
                            captions[Path(fn).name] = {"source_text": tx, "personal_text": "", "text": tx}
        except Exception:
            pass

    if not captions and json_path.exists():
        try:
            payload = _safe_json_loads(json_path.read_text(encoding="utf-8"), {})
            if isinstance(payload, dict):
                for k, v in payload.items():
                    fn = Path(str(k)).name
                    if isinstance(v, dict):
                        src = str(v.get("source_text") or v.get("text") or "").strip()
                        per = str(v.get("personal_text") or "").strip()
                        tx = " ".join([src, per]).strip()
                        if fn and tx:
                            captions[fn] = {"source_text": src, "personal_text": per, "text": tx}
                    else:
                        tx = str(v).strip()
                        if fn and tx:
                            captions[fn] = {"source_text": tx, "personal_text": "", "text": tx}
            elif isinstance(payload, list):
                for row in payload:
                    if not isinstance(row, dict):
                        continue
                    fn = Path(str(row.get("filename") or row.get("file") or "")).name
                    src = str(row.get("source_text") or row.get("text") or row.get("caption") or "").strip()
                    per = str(row.get("personal_text") or "").strip()
                    tx = " ".join([src, per]).strip()
                    if fn and tx:
                        captions[fn] = {"source_text": src, "personal_text": per, "text": tx}
        except Exception:
            pass

    if not captions and txt_path.exists():
        try:
            raw_txt = txt_path.read_text(encoding="utf-8")
            for line in raw_txt.splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                if "\t" in raw:
                    fn, tx = raw.split("\t", 1)
                elif "|" in raw:
                    fn, tx = raw.split("|", 1)
                else:
                    continue
                fn = Path(fn.strip()).name
                tx = tx.strip()
                if fn and tx:
                    captions[fn] = {"source_text": tx, "personal_text": "", "text": tx}
            if not captions:
                blocks = re.split(r"\bImage\s+(\d+)\b", raw_txt, flags=re.IGNORECASE)
                # blocks structure: [prefix, idx1, text1, idx2, text2, ...]
                for i in range(1, len(blocks) - 1, 2):
                    idx = str(blocks[i]).strip()
                    body = str(blocks[i + 1]).strip()
                    if not idx or not body:
                        continue
                    base = f"{int(idx)}"
                    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"):
                        captions[f"{base}{ext}"] = {"source_text": body, "personal_text": "", "text": body}
        except Exception:
            pass
    return captions


def _combined_vector(image_vec: list[float], text_vec: list[float], image_weight: float) -> list[float]:
    if not image_vec and not text_vec:
        return []
    if not image_vec:
        return _vec_normalize(text_vec)
    if not text_vec:
        return _vec_normalize(image_vec)
    iv = _vec_normalize(image_vec)
    tv = _vec_normalize(text_vec)
    w_img = max(0.0, min(1.0, image_weight))
    w_txt = 1.0 - w_img
    combo = [float((iv[i] * w_img) + (tv[i] * w_txt)) for i in range(min(len(iv), len(tv)))]
    return _vec_normalize(combo)


def train_associations_taste_model(
    training_folder: str,
    model_path: str | None = None,
    source_text_weight: float = _SOURCE_TEXT_WEIGHT_DEFAULT,
    personal_text_weight: float = _PERSONAL_TEXT_WEIGHT_DEFAULT,
) -> dict[str, Any]:
    global _TASTE_MODEL_CACHE
    folder = Path(training_folder).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Training folder not found: {folder}")
    files = _sorted_training_images(folder)
    if len(files) < 6:
        raise ValueError("Need at least 6 images in training set.")

    caption_map = _load_training_captions(folder)
    vectors: list[tuple[str, list[float], dict[str, str]]] = []
    embed_type = ""
    for p in files:
        try:
            raw = p.read_bytes()
        except Exception:
            continue
        vec, kind = _image_embedding_from_bytes(raw)
        if not vec:
            continue
        if not embed_type:
            embed_type = kind
        if kind != embed_type:
            continue
        vectors.append((p.name, vec, caption_map.get(p.name, {})))
    if len(vectors) < 6:
        raise ValueError("Could not build enough image embeddings from training set.")

    text_dim = _TEXT_HASH_DIM if caption_map else 0
    image_weight = _IMAGE_WEIGHT_DEFAULT if text_dim > 0 else 1.0
    joint_vectors: list[tuple[str, list[float]]] = []
    source_text_weight = max(0.0, float(source_text_weight))
    personal_text_weight = max(0.0, float(personal_text_weight))
    if source_text_weight + personal_text_weight <= 0.0:
        source_text_weight = _SOURCE_TEXT_WEIGHT_DEFAULT
        personal_text_weight = _PERSONAL_TEXT_WEIGHT_DEFAULT
    total_w = source_text_weight + personal_text_weight
    source_text_weight = source_text_weight / total_w
    personal_text_weight = personal_text_weight / total_w

    for name, vec, cap in vectors:
        if text_dim > 0:
            source_text = str((cap or {}).get("source_text") or "")
            personal_text = str((cap or {}).get("personal_text") or "")
            source_vec = _text_hash_vector(source_text, dim=text_dim)
            personal_vec = _text_hash_vector(personal_text, dim=text_dim)
            tv = _vec_normalize(_vec_add(_vec_scale(source_vec, source_text_weight), _vec_scale(personal_vec, personal_text_weight)))
            jv = _combined_vector(vec, tv, image_weight=image_weight)
        else:
            jv = _vec_normalize(vec)
        if jv:
            joint_vectors.append((name, jv))
    if len(joint_vectors) < 6:
        raise ValueError("Could not build enough joint vectors from training set.")

    pos_deltas: list[list[float]] = []
    for i in range(len(joint_vectors) - 1):
        d = _vec_sub(joint_vectors[i + 1][1], joint_vectors[i][1])
        d = _vec_normalize(d)
        if d:
            pos_deltas.append(d)
    if len(pos_deltas) < 4:
        raise ValueError("Not enough valid transition pairs in training data.")

    neg_deltas: list[list[float]] = []
    rng = random.Random(42)
    idxs = list(range(len(joint_vectors)))
    for i in range(len(joint_vectors) - 1):
        cur = joint_vectors[i][1]
        bad_choices = [j for j in idxs if j != i + 1 and j != i and abs(j - i) > 2]
        rng.shuffle(bad_choices)
        for j in bad_choices[:3]:
            nd = _vec_normalize(_vec_sub(joint_vectors[j][1], cur))
            if nd:
                neg_deltas.append(nd)

    dim = len(pos_deltas[0])
    pos_centroid = [0.0] * dim
    for d in pos_deltas:
        pos_centroid = _vec_add(pos_centroid, d)
    pos_centroid = _vec_normalize(_vec_scale(pos_centroid, 1.0 / max(1, len(pos_deltas))))

    neg_centroid: list[float] = []
    if neg_deltas:
        neg_centroid = [0.0] * dim
        for d in neg_deltas:
            neg_centroid = _vec_add(neg_centroid, d)
        neg_centroid = _vec_normalize(_vec_scale(neg_centroid, 1.0 / max(1, len(neg_deltas))))

    templates = pos_deltas[: min(200, len(pos_deltas))]
    model = {
        "version": 1,
        "trained_at": datetime.utcnow().isoformat() + "Z",
        "training_folder": str(folder),
        "embedding_type": embed_type,
        "text_dim": text_dim,
        "image_weight": image_weight,
        "source_text_weight": source_text_weight,
        "personal_text_weight": personal_text_weight,
        "vector_dim": dim,
        "file_count": len(joint_vectors),
        "pair_count": len(pos_deltas),
        "negative_pair_count": len(neg_deltas),
        "caption_count": len(caption_map),
        "pos_centroid": pos_centroid,
        "neg_centroid": neg_centroid,
        "templates": templates,
    }
    target = Path(model_path).expanduser().resolve() if model_path else _TASTE_MODEL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(model), encoding="utf-8")
    _TASTE_MODEL_CACHE = model
    return model


def _load_taste_model() -> dict[str, Any] | None:
    global _TASTE_MODEL_CACHE
    if _TASTE_MODEL_CACHE is not None:
        return _TASTE_MODEL_CACHE
    try:
        if not _TASTE_MODEL_PATH.exists():
            return None
        payload = _safe_json_loads(_TASTE_MODEL_PATH.read_text(encoding="utf-8"), {})
        if not isinstance(payload, dict):
            return None
        _TASTE_MODEL_CACHE = payload
        return _TASTE_MODEL_CACHE
    except Exception:
        return None


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def train_associations_feedback_model(log_path: str | None = None, model_path: str | None = None) -> dict[str, Any] | None:
    global _FEEDBACK_MODEL_CACHE
    src = Path(log_path).expanduser().resolve() if log_path else _FEEDBACK_LOG_PATH
    if not src.exists():
        return None

    rows: list[dict[str, Any]] = []
    try:
        for line in src.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            row = _safe_json_loads(raw, {})
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return None
    if not rows:
        return None

    label_to_sign = {"good": 1.0, "okay": 0.45, "bad": -1.0, "too_random": -1.0, "too_close": -1.0}
    by_type: dict[str, dict[str, list[list[float]]]] = {"clip": {"pos": [], "neg": []}, "hist": {"pos": [], "neg": []}}
    for row in rows[-1200:]:
        ctx = row.get("context") if isinstance(row.get("context"), dict) else {}
        source = str((ctx or {}).get("source") or "").strip().lower()
        if source in {"bootstrap_training", "smoke", "smoke-test", "test-script"}:
            continue
        label = str(row.get("label") or "").strip().lower()
        sign = label_to_sign.get(label)
        if sign is None:
            continue
        emb = str(row.get("embedding_type") or "").strip().lower()
        delta = row.get("delta") or []
        if emb not in by_type or not isinstance(delta, list):
            continue
        vec = [float(x) for x in delta if isinstance(x, (int, float))]
        if len(vec) < 8:
            continue
        vec = _vec_normalize(vec)
        if not vec:
            continue
        if sign > 0:
            by_type[emb]["pos"].append(vec)
        else:
            by_type[emb]["neg"].append(vec)

    def build_model(emb: str, pos_deltas: list[list[float]], neg_deltas: list[list[float]]) -> dict[str, Any] | None:
        if len(pos_deltas) < 6 or len(neg_deltas) < 6:
            return None
        dim = len(pos_deltas[0])
        pos = [d for d in pos_deltas if len(d) == dim]
        neg = [d for d in neg_deltas if len(d) == dim]
        if len(pos) < 6 or len(neg) < 6:
            return None
        pos_centroid = _vec_normalize(_vec_scale([sum(vals) for vals in zip(*pos)], 1.0 / max(1, len(pos))))
        neg_centroid = _vec_normalize(_vec_scale([sum(vals) for vals in zip(*neg)], 1.0 / max(1, len(neg))))
        if not pos_centroid or not neg_centroid:
            return None
        return {
            "version": 1,
            "trained_at": datetime.utcnow().isoformat() + "Z",
            "embedding_type": emb,
            "vector_dim": dim,
            "feedback_count": len(rows),
            "positive_count": len(pos),
            "negative_count": len(neg),
            "pos_centroid": pos_centroid,
            "neg_centroid": neg_centroid,
            "templates": pos[: min(300, len(pos))],
        }

    clip_model = build_model("clip", by_type["clip"]["pos"], by_type["clip"]["neg"])
    hist_model = build_model("hist", by_type["hist"]["pos"], by_type["hist"]["neg"])
    model = clip_model or hist_model
    if not model:
        return None

    dst = Path(model_path).expanduser().resolve() if model_path else _FEEDBACK_MODEL_PATH
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(model), encoding="utf-8")
    _FEEDBACK_MODEL_CACHE = model
    return model


def _load_feedback_model() -> dict[str, Any] | None:
    global _FEEDBACK_MODEL_CACHE
    if _FEEDBACK_MODEL_CACHE is not None:
        return _FEEDBACK_MODEL_CACHE
    try:
        if not _FEEDBACK_MODEL_PATH.exists():
            return None
        payload = _safe_json_loads(_FEEDBACK_MODEL_PATH.read_text(encoding="utf-8"), {})
        if not isinstance(payload, dict):
            return None
        _FEEDBACK_MODEL_CACHE = payload
        return _FEEDBACK_MODEL_CACHE
    except Exception:
        return None


def _feedback_preference_score(
    model: dict[str, Any] | None,
    current_vec: list[float] | None,
    candidate_vec: list[float] | None,
) -> float:
    if not model or not current_vec or not candidate_vec:
        return 0.5
    if len(current_vec) != len(candidate_vec):
        return 0.5
    vec_dim = int(model.get("vector_dim") or 0)
    if vec_dim <= 0 or len(current_vec) != vec_dim:
        return 0.5
    delta = _vec_normalize(_vec_sub(candidate_vec, current_vec))
    if not delta:
        return 0.5
    pos_centroid = model.get("pos_centroid") or []
    neg_centroid = model.get("neg_centroid") or []
    pos_score = _cosine(delta, pos_centroid) if isinstance(pos_centroid, list) and len(pos_centroid) == vec_dim else 0.0
    neg_score = _cosine(delta, neg_centroid) if isinstance(neg_centroid, list) and len(neg_centroid) == vec_dim else 0.0
    margin = _clamp01((pos_score - neg_score + 1.0) / 2.0)
    templates = model.get("templates") or []
    t_scores: list[float] = []
    if isinstance(templates, list):
        for t in templates[:80]:
            if isinstance(t, list) and len(t) == vec_dim:
                t_scores.append(_cosine(delta, t))
    if t_scores:
        t_scores.sort(reverse=True)
        topk = t_scores[: min(5, len(t_scores))]
        knn = _clamp01((sum(topk) / max(1, len(topk)) + 1.0) / 2.0)
    else:
        knn = 0.5
    return _clamp01((margin * 0.65) + (knn * 0.35))


def record_association_feedback(
    current_url: str,
    candidate_url: str,
    label: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_label = str(label or "").strip().lower()
    if clean_label not in {"good", "okay", "bad", "too_random", "too_close"}:
        raise ValueError("Invalid feedback label.")
    cur = str(current_url or "").strip()
    cand = str(candidate_url or "").strip()
    if not cur or not cand:
        raise ValueError("current_url and candidate_url are required.")

    emb_type = ""
    delta: list[float] = []
    try:
        cur_raw = _fetch_bytes(cur, timeout=12)
        cand_raw = _fetch_bytes(cand, timeout=12)
        cur_vec, cur_kind = _image_embedding_from_bytes(cur_raw or b"")
        cand_vec, cand_kind = _image_embedding_from_bytes(cand_raw or b"")
        if cur_vec and cand_vec and cur_kind and cur_kind == cand_kind and len(cur_vec) == len(cand_vec):
            emb_type = cur_kind
            delta = _vec_normalize(_vec_sub(cand_vec, cur_vec))
    except Exception:
        pass

    payload: dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "label": clean_label,
        "current_url": cur,
        "candidate_url": cand,
        "embedding_type": emb_type,
        "delta": delta,
    }
    if isinstance(context, dict) and context:
        payload["context"] = context
    _append_jsonl(_FEEDBACK_LOG_PATH, payload)

    trained = train_associations_feedback_model()
    return {
        "ok": True,
        "logged": True,
        "trained": bool(trained),
        "feedback_count": int((trained or {}).get("feedback_count") or 0),
        "positive_count": int((trained or {}).get("positive_count") or 0),
        "negative_count": int((trained or {}).get("negative_count") or 0),
    }


def _taste_acceptability_score(
    model: dict[str, Any] | None,
    current_vec: list[float] | None,
    candidate_vec: list[float] | None,
) -> float:
    if not model or not current_vec or not candidate_vec:
        return 0.5
    if len(current_vec) != len(candidate_vec):
        return 0.5
    vec_dim = int(model.get("vector_dim") or 0)
    if vec_dim <= 0 or len(current_vec) != vec_dim:
        return 0.5
    delta = _vec_normalize(_vec_sub(candidate_vec, current_vec))
    if not delta:
        return 0.5

    pos_centroid = model.get("pos_centroid") or []
    neg_centroid = model.get("neg_centroid") or []
    pos_score = _cosine(delta, pos_centroid) if isinstance(pos_centroid, list) and len(pos_centroid) == vec_dim else 0.0
    neg_score = _cosine(delta, neg_centroid) if isinstance(neg_centroid, list) and len(neg_centroid) == vec_dim else 0.0
    margin = _clamp01((pos_score - neg_score + 1.0) / 2.0)

    templates = model.get("templates") or []
    t_scores: list[float] = []
    if isinstance(templates, list):
        for t in templates[:80]:
            if isinstance(t, list) and len(t) == vec_dim:
                t_scores.append(_cosine(delta, t))
    if t_scores:
        t_scores.sort(reverse=True)
        topk = t_scores[: min(5, len(t_scores))]
        knn = _clamp01((sum(topk) / max(1, len(topk)) + 1.0) / 2.0)
    else:
        knn = 0.5
    return _clamp01((margin * 0.62) + (knn * 0.38))


def _taste_runtime_vector(
    model: dict[str, Any] | None,
    image_vec: list[float] | None,
    text: str,
) -> list[float] | None:
    if image_vec is None:
        return None
    if not model:
        return image_vec
    text_dim = int(model.get("text_dim") or 0)
    if text_dim <= 0:
        return image_vec
    image_weight = float(model.get("image_weight") or _IMAGE_WEIGHT_DEFAULT)
    text_vec = _text_hash_vector(text, dim=text_dim)
    return _combined_vector(image_vec, text_vec, image_weight=image_weight)


def _llm_visual_description(api_key: str, image_url: str) -> str:
    if not api_key or not image_url:
        return ""
    client = _openai_client(api_key)
    prompt = (
        "Describe this image for image-search query expansion. "
        "Return strict JSON: {\"subject\":\"...\",\"style\":\"...\",\"mood\":\"...\",\"keywords\":[...]} "
        "with concise values."
    )
    request_image = image_url
    if image_url.startswith("/") or image_url.startswith("file://"):
        raw = _fetch_bytes(image_url, timeout=10)
        if raw:
            b64 = base64.b64encode(raw).decode("ascii")
            request_image = f"data:image/jpeg;base64,{b64}"
    try:
        resp = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": request_image}},
                    ],
                }
            ],
            temperature=0.2,
            max_tokens=180,
            response_format={"type": "json_object"},
        )
        payload = _safe_json_loads(resp.choices[0].message.content or "{}", {})
        subject = str(payload.get("subject") or "").strip()
        style = str(payload.get("style") or "").strip()
        mood = str(payload.get("mood") or "").strip()
        kws = payload.get("keywords") or []
        if not isinstance(kws, list):
            kws = []
        keys = " ".join(str(k).strip() for k in kws[:6] if str(k).strip())
        return " ".join(x for x in [subject, style, mood, keys] if x).strip()
    except Exception:
        return ""


def _llm_expand_query(
    api_key: str,
    seed_title: str,
    current_title: str,
    history_titles: list[str],
    inventiveness: float,
) -> tuple[str, list[str], list[str]]:
    client = _openai_client(api_key)
    prompt = (
        "You create image-association chains for an art composition. "
        "Return strict JSON with keys query, concepts, search_queries. "
        "query should be short (2-8 words), concepts 3-8 ideas, search_queries 4-10 concise phrases.\n"
        f"Seed: {seed_title}\n"
        f"Current: {current_title}\n"
        f"Recent history: {', '.join(history_titles[:8])}\n"
        f"Inventiveness (0..1): {inventiveness:.3f}\n"
        "If inventiveness is high, make conceptual leaps. If low, stay closely related."
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=min(1.1, 0.25 + (inventiveness * 0.9)),
            max_tokens=220,
            response_format={"type": "json_object"},
        )
        payload = _safe_json_loads(resp.choices[0].message.content or "{}", {})
        query = str(payload.get("query") or "").strip()
        concepts = payload.get("concepts") or []
        search_queries = payload.get("search_queries") or []
        if not isinstance(concepts, list):
            concepts = []
        if not isinstance(search_queries, list):
            search_queries = []
        clean_concepts = [str(c).strip() for c in concepts if str(c).strip()][:8]
        clean_queries = [str(q).strip() for q in search_queries if str(q).strip()][:10]
        return (query or current_title or seed_title or "visual culture"), clean_concepts, clean_queries
    except Exception:
        base = current_title or seed_title or "visual culture"
        return base, [], []


def _fallback_associative_directions(
    query: str,
    concepts: list[str],
    pattern_terms: list[str],
    color_terms: list[str],
) -> list[dict[str, Any]]:
    shape_terms = [t for t in pattern_terms if t in _PATTERN_FORM_TERMS][:6]
    color_focus = color_terms[:4]
    concept_focus = [c for c in concepts[:6] if c]
    directions = [
        {
            "id": "form",
            "label": "Form / Structure",
            "query": " ".join((shape_terms or pattern_terms[:5] or [query])[:6]).strip(),
            "focus_terms": shape_terms or pattern_terms[:6],
            "rationale": "Follow repeated shapes/forms and structural details.",
        },
        {
            "id": "color",
            "label": "Color / Atmosphere",
            "query": " ".join((color_focus + pattern_terms[:3] + concepts[:2] + [query])[:7]).strip(),
            "focus_terms": color_focus + pattern_terms[:3],
            "rationale": "Follow palette, brightness, and tonal atmosphere.",
        },
        {
            "id": "concept",
            "label": "Concept / Metaphor",
            "query": " ".join((concept_focus + pattern_terms[:3] + [query])[:8]).strip(),
            "focus_terms": concept_focus + pattern_terms[:3],
            "rationale": "Follow the conceptual bridge and metaphorical leap.",
        },
    ]
    for d in directions:
        d["query"] = str(d.get("query") or query).strip() or query
        ft = d.get("focus_terms") or []
        if not isinstance(ft, list):
            ft = []
        d["focus_terms"] = [str(x).strip() for x in ft if str(x).strip()][:8]
    return directions


def _llm_associative_directions(
    api_key: str,
    seed_title: str,
    current_title: str,
    query: str,
    concepts: list[str],
    pattern_terms: list[str],
    color_terms: list[str],
    inventiveness: float,
) -> list[dict[str, Any]]:
    if not api_key:
        return []
    client = _openai_client(api_key)
    prompt = (
        "Plan exactly three image-association directions.\n"
        "Return strict JSON: {\"directions\":[{\"id\":\"...\",\"label\":\"...\",\"query\":\"...\",\"focus_terms\":[...],\"rationale\":\"...\"}, ...]}.\n"
        "Each direction must be distinct: (1) form/shape, (2) color/atmosphere, (3) concept/metaphor.\n"
        f"Seed context: {seed_title}\n"
        f"Current context: {current_title}\n"
        f"Base query: {query}\n"
        f"Concepts: {', '.join(concepts[:8])}\n"
        f"Pattern terms: {', '.join(pattern_terms[:10])}\n"
        f"Color terms: {', '.join(color_terms[:6])}\n"
        f"Inventiveness (0..1): {inventiveness:.3f}\n"
        "Keep query concise (2-8 words) and focus_terms 3-8 items."
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=min(1.0, 0.25 + (inventiveness * 0.65)),
            max_tokens=420,
            response_format={"type": "json_object"},
        )
        payload = _safe_json_loads(resp.choices[0].message.content or "{}", {})
        rows = payload.get("directions") or []
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for idx, row in enumerate(rows[:3]):
            if not isinstance(row, dict):
                continue
            q = str(row.get("query") or "").strip()
            lbl = str(row.get("label") or "").strip() or f"Direction {idx + 1}"
            rid = str(row.get("id") or "").strip().lower() or f"d{idx + 1}"
            rat = str(row.get("rationale") or "").strip()
            ft = row.get("focus_terms") or []
            if not isinstance(ft, list):
                ft = []
            focus_terms = [str(x).strip() for x in ft if str(x).strip()][:8]
            out.append(
                {
                    "id": rid,
                    "label": lbl,
                    "query": q or query,
                    "focus_terms": focus_terms,
                    "rationale": rat,
                }
            )
        return out[:3]
    except Exception:
        return []


def _generate_candidates_openai(
    api_key: str,
    seed_url: str,
    current_url: str,
    seed_subject_terms: list[str],
    directions: list[dict[str, Any]],
    count: int,
) -> list[Candidate]:
    if not api_key or count <= 0:
        return []
    client = _openai_client(api_key)
    seed_desc = _llm_visual_description(api_key, seed_url) or _url_text_signature(seed_url)
    current_desc = _llm_visual_description(api_key, current_url) or _url_text_signature(current_url)
    out: list[Candidate] = []
    for idx, d in enumerate(directions[: max(1, count)]):
        direction_label = str((d or {}).get("label") or f"Direction {idx + 1}").strip()
        direction_query = str((d or {}).get("query") or "").strip()
        focus_terms = [str(x).strip() for x in ((d or {}).get("focus_terms") or []) if str(x).strip()][:8]
        prompt = (
            "Create one square artistic image that is visually and conceptually associated with a seed image.\n"
            "It must remain connected to the seed subject while making a meaningful associative leap.\n"
            f"Seed visual description: {seed_desc}\n"
            f"Current visual description: {current_desc}\n"
            f"Critical subject anchors: {', '.join(seed_subject_terms[:8])}\n"
            f"Direction: {direction_label}\n"
            f"Direction query: {direction_query}\n"
            f"Direction focus terms: {', '.join(focus_terms)}\n"
            "Avoid logos, text overlays, UI screenshots, memes, ads, and collage artifacts.\n"
            "Output a single coherent square image."
        )
        try:
            resp = client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
            )
            rows = getattr(resp, "data", None) or []
            if not rows:
                continue
            b64 = str(getattr(rows[0], "b64_json", "") or "").strip()
            if not b64:
                continue
            data_url = f"data:image/png;base64,{b64}"
            out.append(
                Candidate(
                    url=data_url,
                    title=f"Generated: {direction_label}",
                    source="generated",
                )
            )
        except Exception:
            continue
    return out[:count]


def _direction_match(
    directions: list[dict[str, Any]],
    candidate_text: str,
    text_score: float,
    llm_score: float,
    pattern_focus: float,
) -> tuple[str, str, float]:
    best_id = ""
    best_label = ""
    best_score = 0.0
    for d in directions[:3]:
        terms = [str(x).strip().lower() for x in (d.get("focus_terms") or []) if str(x).strip()]
        overlap = _token_overlap_score(terms[:8], candidate_text)
        semantic = _clamp01((float(text_score) * 0.46) + (float(llm_score) * 0.34) + (float(pattern_focus) * 0.2))
        score = _clamp01((overlap * 0.62) + (semantic * 0.38))
        if score > best_score:
            best_score = score
            best_id = str(d.get("id") or "").strip()
            best_label = str(d.get("label") or "").strip()
    return best_id, best_label, best_score


def _query_looks_local_artifact(query: str) -> bool:
    q = (query or "").lower().strip()
    if not q:
        return True
    bad_signals = ["127 0 0 1", "localhost", "training set media", "source media"]
    if any(sig in q for sig in bad_signals):
        return True
    tokens = [t for t in re.split(r"\s+", q) if t]
    alpha_tokens = [t for t in tokens if re.search(r"[a-z]", t)]
    ext_hits = sum(1 for t in alpha_tokens if t in {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "avif"})
    if len(alpha_tokens) <= 2 or ext_hits >= 2:
        return True
    return False


def _normalize_query_for_web(query: str, seed_title: str, current_title: str) -> str:
    q = (query or "").strip()
    if not _query_looks_local_artifact(q):
        return q
    seed = (seed_title or "").strip()
    cur = (current_title or "").strip()
    fallback_parts = [x for x in [seed, cur] if x and not _query_looks_local_artifact(x)]
    if fallback_parts:
        return " ".join(fallback_parts[:2]).strip()
    return "contemporary art photography illustration visual culture"


def _web_candidates_wikimedia(query: str, limit: int = 30) -> list[Candidate]:
    params = {
        "action": "query",
        "format": "json",
        "origin": "*",
        "generator": "search",
        "gsrsearch": query.strip() or "visual culture",
        "gsrnamespace": 6,
        "gsrlimit": max(10, min(50, limit)),
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": 1600,
    }
    url = f"{WIKIMEDIA_API}?{urlencode(params)}"
    body = _fetch_bytes(url, timeout=14)
    if not body:
        return []
    payload = _safe_json_loads(body.decode("utf-8", errors="ignore"), {})
    pages = (payload.get("query") or {}).get("pages") or {}
    out: list[Candidate] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [])
        if not info:
            continue
        entry = info[0] or {}
        mime = str(entry.get("mime") or "").lower()
        if not mime.startswith("image/"):
            continue
        # Wikimedia can rate-limit direct original-media fetches; prefer API-provided thumbnail URLs.
        raw_url = str(entry.get("thumburl") or entry.get("url") or "").strip()
        if not raw_url:
            continue
        title = str(page.get("title") or "").replace("File:", "").strip()
        out.append(Candidate(url=raw_url, title=title or "wikimedia image", source="wikimedia"))
        if len(out) >= limit:
            break
    return out


def _web_candidates_openverse(query: str, limit: int = 30) -> list[Candidate]:
    params = {
        "q": query.strip() or "visual culture",
        "page_size": max(10, min(60, limit)),
        "mature": "false",
    }
    url = f"{OPENVERSE_API}?{urlencode(params)}"
    body = _fetch_bytes(url, timeout=14)
    if not body:
        return []
    payload = _safe_json_loads(body.decode("utf-8", errors="ignore"), {})
    rows = payload.get("results") or []
    if not isinstance(rows, list):
        return []
    out: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Prefer source/original URL over thumbnail for real analysis.
        raw_url = str(row.get("url") or row.get("thumbnail") or "").strip()
        if not raw_url:
            continue
        lower = raw_url.lower()
        if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"]):
            continue
        title = str(row.get("title") or row.get("id") or "").strip() or "openverse image"
        out.append(Candidate(url=raw_url, title=title, source="openverse"))
        if len(out) >= limit:
            break
    return out


def _web_candidates_flickr_public(query: str, limit: int = 24) -> list[Candidate]:
    tags = ",".join([t for t in re.split(r"\s+", query.strip()) if t][:6]) or "art,visual,culture"
    params = {
        "format": "json",
        "nojsoncallback": "1",
        "tags": tags,
    }
    url = f"{FLICKR_PUBLIC_API}?{urlencode(params)}"
    body = _fetch_bytes(url, timeout=14)
    if not body:
        return []
    payload = _safe_json_loads(body.decode("utf-8", errors="ignore"), {})
    rows = payload.get("items") or []
    if not isinstance(rows, list):
        return []
    out: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        media = row.get("media") or {}
        if not isinstance(media, dict):
            media = {}
        raw_url = str(media.get("m") or "").strip()
        if not raw_url:
            continue
        # Flickr feed returns medium thumbnail (_m). Promote to larger source when possible.
        raw_url = re.sub(r"_m(\.[a-z0-9]+)$", r"_b\1", raw_url, flags=re.IGNORECASE)
        title = str(row.get("title") or "").strip() or "flickr image"
        out.append(Candidate(url=raw_url, title=title, source="flickr"))
        if len(out) >= limit:
            break
    return out


def _rank_with_openai_embeddings(
    api_key: str,
    query_text: str,
    candidates: list[Candidate],
    inventiveness: float,
) -> dict[str, float]:
    if not candidates:
        return {}
    client = _openai_client(api_key)
    doc_texts = [f"{c.title} {_url_text_signature(c.url)}".strip() for c in candidates]
    try:
        q_emb = client.embeddings.create(model=OPENAI_EMBED_MODEL, input=[query_text]).data[0].embedding
        d_emb = client.embeddings.create(model=OPENAI_EMBED_MODEL, input=doc_texts).data
        scores: dict[str, float] = {}
        for idx, item in enumerate(candidates):
            sim = _cosine(q_emb, d_emb[idx].embedding)
            if inventiveness > 0.66:
                sim = (sim * 0.72) + ((1.0 - max(-1.0, min(1.0, sim))) * 0.18)
            scores[item.url] = float(sim)
        return scores
    except Exception:
        return {}


def _llm_concept_scores(
    api_key: str,
    query_text: str,
    candidates: list[Candidate],
    inventiveness: float,
) -> dict[str, float]:
    if not candidates:
        return {}
    client = _openai_client(api_key)
    rows = [{"i": i, "title": c.title, "url": c.url} for i, c in enumerate(candidates[:40])]
    prompt = (
        "Score how strongly each candidate is conceptually/aesthetically associated with the query. "
        "Return strict JSON object: {\"scores\":[{\"i\":<int>,\"s\":<0..1 float>}, ...]}. "
        "Higher inventiveness should reward surprising but meaningful links.\n"
        f"Query: {query_text}\n"
        f"Inventiveness: {inventiveness:.3f}\n"
        f"Candidates: {json.dumps(rows)}"
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=min(1.0, 0.2 + (inventiveness * 0.8)),
            max_tokens=320,
            response_format={"type": "json_object"},
        )
        payload = _safe_json_loads(resp.choices[0].message.content or "{}", {})
        out: dict[str, float] = {}
        for row in payload.get("scores") or []:
            i = int(row.get("i"))
            s = float(row.get("s"))
            if 0 <= i < len(candidates):
                out[candidates[i].url] = max(0.0, min(1.0, s))
        return out
    except Exception:
        return {}


def _build_candidate_features(candidates: list[Candidate], max_count: int = 96) -> list[CandidateFeatures]:
    out: list[CandidateFeatures] = []
    for cand in candidates[:max_count]:
        # Spend more time fetching larger source images for better feature quality.
        raw = _fetch_bytes(cand.url, timeout=16)
        if not raw:
            continue
        w, h = _image_size_from_bytes(raw)
        if min(w, h) < 220:
            continue
        out.append(
            CandidateFeatures(
                candidate=cand,
                hist_vec=_image_visual_vector_from_bytes(raw),
                hash_bits=_image_hash64_from_bytes(raw),
                clip_vec=_clip_embed_image_bytes(raw),
            )
        )
    return out


def pick_association_candidate(
    seed_url: str,
    current_url: str,
    history_urls: list[str],
    local_candidates: list[dict[str, Any]],
    inventiveness: float = 0.55,
    api_key: str = "",
    personal_influence: float = _PERSONAL_TEXT_WEIGHT_DEFAULT,
) -> dict[str, Any] | None:
    inventiveness = max(0.0, min(1.0, float(inventiveness or 0.0)))
    personal_influence = _clamp01(float(personal_influence or 0.0))
    history_urls = [str(u).strip() for u in history_urls if str(u).strip()]
    seen = set(history_urls[-90:])
    seen.add(current_url)

    seed_title = _url_text_signature(seed_url) or "seed image"
    current_title = _url_text_signature(current_url) or seed_title
    seed_caption = _training_caption_for_url(seed_url)
    current_caption = _training_caption_for_url(current_url)
    if seed_caption:
        seed_title = seed_caption if _query_looks_local_artifact(seed_title) else " ".join([seed_title, seed_caption]).strip()
    if current_caption:
        current_title = current_caption if _query_looks_local_artifact(current_title) else " ".join([current_title, current_caption]).strip()
    history_titles = [_url_text_signature(u) for u in history_urls[-12:]]
    current_raw = _fetch_bytes(current_url, timeout=10) if current_url else None
    current_hist = _image_visual_vector_from_bytes(current_raw) if current_raw else None
    current_hash = _image_hash64_from_bytes(current_raw) if current_raw else ""
    current_clip = _clip_embed_image_bytes(current_raw) if current_raw else None

    query = current_title or seed_title or "visual culture"
    concepts: list[str] = []
    expanded_queries: list[str] = []
    visual_context = _llm_visual_description(api_key, current_url) if api_key else ""
    if visual_context:
        query = visual_context
    if api_key:
        query, concepts, expanded_queries = _llm_expand_query(api_key, seed_title, query, history_titles, inventiveness)
    query = _normalize_query_for_web(query, seed_title=seed_title, current_title=current_title)
    pattern_terms, color_terms = _pattern_focus_terms(
        seed_title=seed_title,
        current_title=current_title,
        query=query,
        concepts=concepts,
        visual_context=visual_context,
        current_hist=current_hist,
    )
    directions = _llm_associative_directions(
        api_key=api_key,
        seed_title=seed_title,
        current_title=current_title,
        query=query,
        concepts=concepts,
        pattern_terms=pattern_terms,
        color_terms=color_terms,
        inventiveness=inventiveness,
    )
    if len(directions) < 3:
        directions = _fallback_associative_directions(
            query=query,
            concepts=concepts,
            pattern_terms=pattern_terms,
            color_terms=color_terms,
        )

    pattern_queries: list[str] = []
    if pattern_terms:
        pattern_queries.append(" ".join(pattern_terms[:5]))
    if color_terms:
        pattern_queries.append(" ".join(color_terms + pattern_terms[:3]))
    search_queries = [query] + concepts[:4] + expanded_queries[:8] + pattern_queries[:2]
    web_candidates: list[Candidate] = []
    for sq in search_queries:
        if not sq:
            continue
        web_candidates.extend(_web_candidates_openverse(sq, limit=20))
        web_candidates.extend(_web_candidates_flickr_public(sq, limit=14))
        web_candidates.extend(_web_candidates_wikimedia(sq, limit=26))
        if len(web_candidates) >= 180:
            break

    local_pool: list[Candidate] = []
    for item in local_candidates or []:
        u = str((item or {}).get("url") or "").strip()
        if not u:
            continue
        n = str((item or {}).get("name") or "").strip() or _url_text_signature(u)
        local_pool.append(Candidate(url=u, title=n, source="local"))

    merged: list[Candidate] = []
    used: set[str] = set()
    for c in (web_candidates + local_pool):
        if not c.url or c.url in used or c.url in seen:
            continue
        used.add(c.url)
        merged.append(c)
    if not merged:
        return None
    if len(merged) > 180:
        merged = merged[:180]

    features = _build_candidate_features(merged, max_count=120)
    if not features:
        return None
    ranked_candidates = [f.candidate for f in features]
    text_scores = _rank_with_openai_embeddings(api_key, " ".join([query] + concepts).strip() or query, ranked_candidates, inventiveness) if api_key else {}
    llm_scores = _llm_concept_scores(api_key, query, ranked_candidates, inventiveness) if api_key else {}

    has_current_visual = bool(current_hist or current_clip)
    taste_model = _load_taste_model()
    feedback_model = _load_feedback_model()
    taste_embedding_type = str((taste_model or {}).get("embedding_type") or "").strip().lower()
    feedback_embedding_type = str((feedback_model or {}).get("embedding_type") or "").strip().lower()
    current_text_signal = " ".join([current_title, query] + concepts[:4]).strip()
    current_taste_vec: list[float] | None = None
    current_feedback_vec: list[float] | None = None
    if taste_embedding_type == "clip":
        current_taste_vec = _taste_runtime_vector(taste_model, current_clip, current_text_signal)
    elif taste_embedding_type == "hist":
        current_taste_vec = _taste_runtime_vector(taste_model, current_hist, current_text_signal)
    if feedback_embedding_type == "clip":
        current_feedback_vec = current_clip
    elif feedback_embedding_type == "hist":
        current_feedback_vec = current_hist

    visual_scores: dict[str, float] = {}
    distinct_ok: dict[str, bool] = {}
    taste_scores: dict[str, float] = {}
    feedback_scores: dict[str, float] = {}
    for feat in features:
        c = feat.candidate
        hist_sim = _cosine(current_hist, feat.hist_vec) if current_hist and feat.hist_vec else 0.0
        clip_sim = _cosine(current_clip, feat.clip_vec) if current_clip and feat.clip_vec else hist_sim
        sim = (hist_sim * 0.35) + (clip_sim * 0.65)
        visual_scores[c.url] = max(0.0, min(1.0, sim))
        hd = _hamming_bits(current_hash, feat.hash_bits) if current_hash and feat.hash_bits else 32
        distinct_ok[c.url] = (hd > 6 and sim < 0.97) if has_current_visual else True
        candidate_text = f"{c.title} {_url_text_signature(c.url)}".strip()
        if taste_embedding_type == "clip":
            candidate_taste_vec = _taste_runtime_vector(taste_model, feat.clip_vec, candidate_text)
            taste_scores[c.url] = _taste_acceptability_score(taste_model, current_taste_vec, candidate_taste_vec)
        elif taste_embedding_type == "hist":
            candidate_taste_vec = _taste_runtime_vector(taste_model, feat.hist_vec, candidate_text)
            taste_scores[c.url] = _taste_acceptability_score(taste_model, current_taste_vec, candidate_taste_vec)
        else:
            taste_scores[c.url] = 0.5
        if feedback_embedding_type == "clip":
            feedback_scores[c.url] = _feedback_preference_score(feedback_model, current_feedback_vec, feat.clip_vec)
        elif feedback_embedding_type == "hist":
            feedback_scores[c.url] = _feedback_preference_score(feedback_model, current_feedback_vec, feat.hist_vec)
        else:
            feedback_scores[c.url] = 0.5

    continuity_weight = 1.0 - inventiveness
    w_visual = 0.34 + (continuity_weight * 0.2)
    w_text = 0.25 + (continuity_weight * 0.2)
    w_llm = 0.2 + (inventiveness * 0.22)
    w_novelty = 0.14 + (inventiveness * 0.2)
    # Personal influence controls how much learned personal preference shapes picks.
    w_taste_base = 0.27 + (continuity_weight * 0.22)
    w_taste = min(0.45, w_taste_base * (0.25 + (personal_influence * 1.35)))
    feedback_count = int((feedback_model or {}).get("feedback_count") or 0)
    feedback_strength = _clamp01(feedback_count / 80.0)
    w_feedback = min(0.3, (0.05 + (0.18 * feedback_strength)) * (0.35 + (0.65 * personal_influence)))
    target_sim = 0.7 - (inventiveness * 0.18)

    scored: list[tuple[float, Candidate]] = []
    for idx, feat in enumerate(features):
        cand = feat.candidate
        if not distinct_ok.get(cand.url, True):
            continue
        visual = visual_scores.get(cand.url, 0.22)
        text = text_scores.get(cand.url, 0.35)
        llm = llm_scores.get(cand.url, 0.4 if api_key else 0.2)
        candidate_text = f"{cand.title} {_url_text_signature(cand.url)}".strip()
        pattern_focus, color_resonance = _pattern_relevance_score(
            pattern_terms=pattern_terms,
            candidate_text=candidate_text,
            current_hist=current_hist,
            candidate_hist=feat.hist_vec,
            text_score=text,
            llm_score=llm,
        )
        novelty = 0.02 if cand.url in seen else 1.0
        taste = taste_scores.get(cand.url, 0.5)
        feedback = feedback_scores.get(cand.url, 0.5)
        proximity = max(0.0, 1.0 - abs(visual - target_sim) / 0.6)
        if has_current_visual and visual < 0.22:
            proximity *= 0.22
        if has_current_visual and visual > 0.94:
            proximity *= 0.15
        jitter = random.random() * 0.05
        total = (proximity * w_visual) + (text * w_text) + (llm * w_llm) + (pattern_focus * 0.16) + (color_resonance * 0.06) + (novelty * w_novelty) + (taste * w_taste) + (feedback * w_feedback) + jitter
        total += ((len(features) - idx) / max(1, len(features))) * (0.03 if inventiveness < 0.5 else 0.01)
        scored.append((total, cand))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: min(10, len(scored))]
    if not top:
        return None
    # Public playback should feel intentional. Exploration belongs in the
    # compare UI; the single-next endpoint returns the strongest candidate.
    picked_tuple = top[0]
    picked_score, picked = picked_tuple
    return {
        "url": picked.url,
        "title": picked.title,
        "source": picked.source,
        "query": query,
        "concepts": concepts[:6],
        "visual_context": visual_context,
        "seed_summary": seed_title,
        "current_summary": current_title,
        "pattern_terms": pattern_terms[:8],
        "color_terms": color_terms[:4],
        "inventiveness": round(float(inventiveness), 3),
        "personal_influence": round(float(personal_influence), 3),
        "picked_score": round(float(picked_score), 4),
        "candidate_pool_size": len(merged),
        "directions": directions[:3],
    }


def pick_association_candidates(
    seed_url: str,
    current_url: str,
    history_urls: list[str],
    local_candidates: list[dict[str, Any]],
    inventiveness: float = 0.55,
    api_key: str = "",
    personal_influence: float = _PERSONAL_TEXT_WEIGHT_DEFAULT,
    count: int = 3,
    enforce_leap: bool = False,
    leap_strength: float = 0.75,
    generation_mode: str = "retrieve",
) -> dict[str, Any]:
    inventiveness = max(0.0, min(1.0, float(inventiveness or 0.0)))
    personal_influence = _clamp01(float(personal_influence or 0.0))
    count = max(1, min(8, int(count or 3)))
    leap_strength = _clamp01(float(leap_strength or 0.0))
    generation_mode = str(generation_mode or "retrieve").strip().lower()
    history_urls = [str(u).strip() for u in history_urls if str(u).strip()]
    seen = set(history_urls[-90:])
    seen.add(current_url)

    seed_title = _url_text_signature(seed_url) or "seed image"
    current_title = _url_text_signature(current_url) or seed_title
    seed_caption = _training_caption_for_url(seed_url)
    current_caption = _training_caption_for_url(current_url)
    if seed_caption:
        seed_title = seed_caption if _query_looks_local_artifact(seed_title) else " ".join([seed_title, seed_caption]).strip()
    if current_caption:
        current_title = current_caption if _query_looks_local_artifact(current_title) else " ".join([current_title, current_caption]).strip()
    history_titles = [_url_text_signature(u) for u in history_urls[-12:]]
    current_raw = _fetch_bytes(current_url, timeout=10) if current_url else None
    current_hist = _image_visual_vector_from_bytes(current_raw) if current_raw else None
    current_hash = _image_hash64_from_bytes(current_raw) if current_raw else ""
    current_clip = _clip_embed_image_bytes(current_raw) if current_raw else None

    query = current_title or seed_title or "visual culture"
    concepts: list[str] = []
    expanded_queries: list[str] = []
    visual_context = _llm_visual_description(api_key, current_url) if api_key else ""
    if visual_context:
        query = visual_context
    if api_key:
        query, concepts, expanded_queries = _llm_expand_query(api_key, seed_title, query, history_titles, inventiveness)
    if visual_context:
        # Keep concrete seed grounding even if LLM query becomes abstract/generic.
        query = " ".join([visual_context, query]).strip()
    seed_subject_terms = _seed_subject_terms(
        seed_title=seed_title,
        current_title=current_title,
        visual_context=visual_context,
        seed_caption=seed_caption,
        current_caption=current_caption,
    )
    query = _normalize_query_for_web(query, seed_title=seed_title, current_title=current_title)
    anchor_terms = _anchor_terms_from_seed(
        seed_title=seed_title,
        current_title=current_title,
        query=" ".join([query, visual_context, " ".join(concepts[:4])]).strip(),
    )
    pattern_terms, color_terms = _pattern_focus_terms(
        seed_title=seed_title,
        current_title=current_title,
        query=query,
        concepts=concepts,
        visual_context=visual_context,
        current_hist=current_hist,
    )
    directions = _llm_associative_directions(
        api_key=api_key,
        seed_title=seed_title,
        current_title=current_title,
        query=query,
        concepts=concepts,
        pattern_terms=pattern_terms,
        color_terms=color_terms,
        inventiveness=inventiveness,
    )
    if len(directions) < 3:
        directions = _fallback_associative_directions(
            query=query,
            concepts=concepts,
            pattern_terms=pattern_terms,
            color_terms=color_terms,
        )
    generated_candidates: list[Candidate] = []
    if generation_mode in {"generated", "hybrid"}:
        generated_candidates = _generate_candidates_openai(
            api_key=api_key,
            seed_url=seed_url,
            current_url=current_url,
            seed_subject_terms=seed_subject_terms,
            directions=directions,
            count=max(count, 3),
        )
    anchor_threshold = 0.09 if enforce_leap else 0.05

    pattern_queries: list[str] = []
    if pattern_terms:
        pattern_queries.append(" ".join(pattern_terms[:6]))
    if color_terms:
        pattern_queries.append(" ".join(color_terms + pattern_terms[:3]))
    direction_queries = [str((d or {}).get("query") or "").strip() for d in directions[:3]]
    subject_query = " ".join(seed_subject_terms[:6]).strip()
    search_queries = [query] + ([subject_query] if subject_query else []) + direction_queries + concepts[:4] + expanded_queries[:8] + pattern_queries[:2]
    dedup_queries: list[str] = []
    for sq in search_queries:
        clean = str(sq or "").strip()
        if clean and clean not in dedup_queries:
            dedup_queries.append(clean)
    search_queries = dedup_queries
    web_candidates: list[Candidate] = []
    if generation_mode != "generated":
        for sq in search_queries:
            if not sq:
                continue
            web_candidates.extend(_web_candidates_openverse(sq, limit=20))
            web_candidates.extend(_web_candidates_flickr_public(sq, limit=14))
            web_candidates.extend(_web_candidates_wikimedia(sq, limit=26))
            if len(web_candidates) >= 180:
                break

    local_pool: list[Candidate] = []
    for item in local_candidates or []:
        u = str((item or {}).get("url") or "").strip()
        if not u:
            continue
        n = str((item or {}).get("name") or "").strip() or _url_text_signature(u)
        local_pool.append(Candidate(url=u, title=n, source="local"))

    merged: list[Candidate] = []
    used: set[str] = set()
    for c in (generated_candidates + web_candidates + local_pool):
        if not c.url or c.url in used or c.url in seen:
            continue
        used.add(c.url)
        merged.append(c)
    if not merged:
        return {"candidates": [], "query": query, "concepts": concepts[:6]}
    if len(merged) > 180:
        merged = merged[:180]

    features = _build_candidate_features(merged, max_count=120)
    ranked_candidates = [f.candidate for f in features] if features else merged[:80]
    text_scores = _rank_with_openai_embeddings(api_key, " ".join([query] + concepts).strip() or query, ranked_candidates, inventiveness) if api_key else {}
    llm_scores = _llm_concept_scores(api_key, query, ranked_candidates, inventiveness) if api_key else {}

    if not features:
        coarse: list[dict[str, Any]] = []
        for c in ranked_candidates:
            text = float(text_scores.get(c.url, 0.35))
            llm = float(llm_scores.get(c.url, 0.4 if api_key else 0.2))
            candidate_text = f"{c.title} {_url_text_signature(c.url)}"
            anchor_match = _anchor_relevance_score(anchor_terms, candidate_text, text, llm)
            pattern_focus, color_resonance = _pattern_relevance_score(
                pattern_terms=pattern_terms,
                candidate_text=candidate_text,
                current_hist=current_hist,
                candidate_hist=None,
                text_score=text,
                llm_score=llm,
            )
            direction_id, direction_label, direction_match = _direction_match(
                directions=directions,
                candidate_text=candidate_text,
                text_score=text,
                llm_score=llm,
                pattern_focus=pattern_focus,
            )
            subject_overlap = _token_overlap_score(seed_subject_terms, candidate_text) if seed_subject_terms else 0.0
            if len(history_urls) == 0 and bool(visual_context.strip()) and len(seed_subject_terms) >= 3 and subject_overlap < 0.08:
                continue
            novelty = 0.02 if c.url in seen else 1.0
            score = (text * 0.36) + (llm * 0.24) + (anchor_match * 0.2) + (pattern_focus * 0.16) + (novelty * 0.04)
            coarse.append(
                {
                    "url": c.url,
                    "title": c.title,
                    "source": c.source,
                    "score": round(float(score), 4),
                    "reason": "internet conceptual match + seed-anchor overlap",
                    "direction_id": direction_id,
                    "direction_label": direction_label,
                    "breakdown": {
                        "visual_proximity": 0.0,
                        "visual_similarity": 0.0,
                        "motif_resonance": 0.0,
                        "text_relevance": round(text, 4),
                        "llm_concept": round(llm, 4),
                        "anchor_match": round(anchor_match, 4),
                        "seed_subject_overlap": round(subject_overlap, 4),
                        "pattern_focus": round(pattern_focus, 4),
                        "color_resonance": round(color_resonance, 4),
                        "direction_match": round(direction_match, 4),
                        "concept_bridge": round((text * 0.45) + (llm * 0.55), 4),
                        "novelty": round(float(novelty), 4),
                        "taste": 0.5,
                        "feedback_pref": 0.5,
                        "leap_bonus": 0.0,
                    },
                }
            )
        coarse.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return {
            "candidates": coarse[:count],
            "query": query,
            "concepts": concepts[:6],
            "anchors": anchor_terms[:8],
            "patterns": pattern_terms[:10],
            "pattern_colors": color_terms[:4],
            "directions": directions[:3],
            "diagnostics": {
                "mode": "text_only_fallback",
                "anchor_threshold": anchor_threshold,
                "total_ranked": len(ranked_candidates),
            },
        }

    has_current_visual = bool(current_hist or current_clip)
    seed_raw = _fetch_bytes(seed_url, timeout=10) if seed_url else None
    seed_hist = _image_visual_vector_from_bytes(seed_raw) if seed_raw else current_hist
    seed_clip = _clip_embed_image_bytes(seed_raw) if seed_raw else current_clip
    taste_model = _load_taste_model()
    feedback_model = _load_feedback_model()
    taste_embedding_type = str((taste_model or {}).get("embedding_type") or "").strip().lower()
    feedback_embedding_type = str((feedback_model or {}).get("embedding_type") or "").strip().lower()
    current_text_signal = " ".join([current_title, query] + concepts[:4]).strip()
    current_taste_vec: list[float] | None = None
    current_feedback_vec: list[float] | None = None
    if taste_embedding_type == "clip":
        current_taste_vec = _taste_runtime_vector(taste_model, current_clip, current_text_signal)
    elif taste_embedding_type == "hist":
        current_taste_vec = _taste_runtime_vector(taste_model, current_hist, current_text_signal)
    if feedback_embedding_type == "clip":
        current_feedback_vec = current_clip
    elif feedback_embedding_type == "hist":
        current_feedback_vec = current_hist

    visual_scores: dict[str, float] = {}
    seed_visual_scores: dict[str, float] = {}
    distinct_ok: dict[str, bool] = {}
    taste_scores: dict[str, float] = {}
    feedback_scores: dict[str, float] = {}
    for feat in features:
        c = feat.candidate
        hist_sim = _cosine(current_hist, feat.hist_vec) if current_hist and feat.hist_vec else 0.0
        clip_sim = _cosine(current_clip, feat.clip_vec) if current_clip and feat.clip_vec else hist_sim
        sim = (hist_sim * 0.35) + (clip_sim * 0.65)
        visual_scores[c.url] = _clamp01(sim)
        seed_hist_sim = _cosine(seed_hist, feat.hist_vec) if seed_hist and feat.hist_vec else 0.0
        seed_clip_sim = _cosine(seed_clip, feat.clip_vec) if seed_clip and feat.clip_vec else seed_hist_sim
        seed_visual_scores[c.url] = _clamp01((seed_hist_sim * 0.35) + (seed_clip_sim * 0.65))
        hd = _hamming_bits(current_hash, feat.hash_bits) if current_hash and feat.hash_bits else 32
        if enforce_leap and has_current_visual:
            distinct_ok[c.url] = hd > 10 and sim < 0.88
        else:
            distinct_ok[c.url] = hd > 6 and sim < 0.97
        candidate_text = f"{c.title} {_url_text_signature(c.url)}".strip()
        if taste_embedding_type == "clip":
            candidate_taste_vec = _taste_runtime_vector(taste_model, feat.clip_vec, candidate_text)
            taste_scores[c.url] = _taste_acceptability_score(taste_model, current_taste_vec, candidate_taste_vec)
        elif taste_embedding_type == "hist":
            candidate_taste_vec = _taste_runtime_vector(taste_model, feat.hist_vec, candidate_text)
            taste_scores[c.url] = _taste_acceptability_score(taste_model, current_taste_vec, candidate_taste_vec)
        else:
            taste_scores[c.url] = 0.5
        if feedback_embedding_type == "clip":
            feedback_scores[c.url] = _feedback_preference_score(feedback_model, current_feedback_vec, feat.clip_vec)
        elif feedback_embedding_type == "hist":
            feedback_scores[c.url] = _feedback_preference_score(feedback_model, current_feedback_vec, feat.hist_vec)
        else:
            feedback_scores[c.url] = 0.5

    continuity_weight = 1.0 - inventiveness
    w_visual = 0.34 + (continuity_weight * 0.2)
    w_text = 0.25 + (continuity_weight * 0.2)
    w_llm = 0.2 + (inventiveness * 0.22)
    w_novelty = 0.14 + (inventiveness * 0.2)
    w_taste_base = 0.27 + (continuity_weight * 0.22)
    w_taste = min(0.45, w_taste_base * (0.25 + (personal_influence * 1.35)))
    feedback_count = int((feedback_model or {}).get("feedback_count") or 0)
    feedback_strength = _clamp01(feedback_count / 80.0)
    w_feedback = min(0.3, (0.05 + (0.18 * feedback_strength)) * (0.35 + (0.65 * personal_influence)))
    target_sim = 0.7 - (inventiveness * 0.18)
    leap_min_sim = 0.24 + (0.14 * leap_strength)
    leap_max_sim = 0.93 - (0.12 * leap_strength)

    scored: list[dict[str, Any]] = []
    rejected_by_anchor = 0
    rejected_by_seed_subject = 0
    rejected_by_leap_band = 0
    rejected_by_distinct = 0
    first_hop = len(history_urls) == 0
    # Always enforce seed-subject grounding on first hop when we have subject terms.
    strict_subject_gate = first_hop and len(seed_subject_terms) >= 2
    for idx, feat in enumerate(features):
        cand = feat.candidate
        if not distinct_ok.get(cand.url, True):
            rejected_by_distinct += 1
            continue
        visual = visual_scores.get(cand.url, 0.22)
        if enforce_leap and has_current_visual and (visual < leap_min_sim or visual > leap_max_sim):
            rejected_by_leap_band += 1
            continue
        candidate_text = f"{cand.title} {_url_text_signature(cand.url)}".strip()
        text = text_scores.get(cand.url, 0.35)
        llm = llm_scores.get(cand.url, 0.4 if api_key else 0.2)
        anchor_match = _anchor_relevance_score(anchor_terms, candidate_text, text, llm)
        pattern_focus, color_resonance = _pattern_relevance_score(
            pattern_terms=pattern_terms,
            candidate_text=candidate_text,
            current_hist=current_hist,
            candidate_hist=feat.hist_vec,
            text_score=text,
            llm_score=llm,
        )
        direction_id, direction_label, direction_match = _direction_match(
            directions=directions,
            candidate_text=candidate_text,
            text_score=text,
            llm_score=llm,
            pattern_focus=pattern_focus,
        )
        seed_visual = seed_visual_scores.get(cand.url, visual)
        motif_resonance = _clamp01(min(visual, seed_visual))
        novelty = 0.02 if cand.url in seen else 1.0
        taste = taste_scores.get(cand.url, 0.5)
        feedback = feedback_scores.get(cand.url, 0.5)
        subject_overlap = _token_overlap_score(seed_subject_terms, candidate_text) if seed_subject_terms else 0.0
        if strict_subject_gate:
            # First hop must stay grounded in seed subject (e.g. ghee/cow/bowl), not only generic texture/style.
            if subject_overlap < 0.08:
                rejected_by_seed_subject += 1
                continue
        if anchor_match < anchor_threshold and pattern_focus < max(0.18, anchor_threshold * 1.8):
            rejected_by_anchor += 1
            continue
        proximity = max(0.0, 1.0 - abs(visual - target_sim) / 0.6)
        if visual < 0.22:
            proximity *= 0.22
        if visual > 0.94:
            proximity *= 0.15
        ordering_bonus = ((len(features) - idx) / max(1, len(features))) * (0.03 if inventiveness < 0.5 else 0.01)
        concept_bridge = (text * 0.45) + (llm * 0.55)
        leap_bonus = 0.0
        if enforce_leap and has_current_visual:
            # Reward "connected but not near-duplicate" transitions.
            leap_center = 0.62 - (inventiveness * 0.08)
            leap_bonus = max(0.0, 1.0 - abs(visual - leap_center) / 0.42) * (0.08 + (0.1 * leap_strength))
        total = (
            (proximity * w_visual)
            + (text * w_text)
            + (llm * w_llm)
            + (anchor_match * 0.14)
            + (pattern_focus * 0.12)
            + (color_resonance * 0.05)
            + (direction_match * 0.08)
            + (motif_resonance * 0.1)
            + (novelty * w_novelty)
            + (taste * w_taste)
            + (feedback * w_feedback)
            + ordering_bonus
            + leap_bonus
            + (concept_bridge * 0.04)
        )
        scored.append(
            {
                "candidate": cand,
                "score": float(total),
                "direction_id": direction_id,
                "direction_label": direction_label,
                "breakdown": {
                    "visual_proximity": round(float(proximity), 4),
                    "visual_similarity": round(float(visual), 4),
                    "motif_resonance": round(float(motif_resonance), 4),
                    "text_relevance": round(float(text), 4),
                    "llm_concept": round(float(llm), 4),
                    "anchor_match": round(float(anchor_match), 4),
                    "seed_subject_overlap": round(float(subject_overlap), 4),
                    "pattern_focus": round(float(pattern_focus), 4),
                    "color_resonance": round(float(color_resonance), 4),
                    "direction_match": round(float(direction_match), 4),
                    "concept_bridge": round(float(concept_bridge), 4),
                    "novelty": round(float(novelty), 4),
                    "taste": round(float(taste), 4),
                    "feedback_pref": round(float(feedback), 4),
                    "leap_bonus": round(float(leap_bonus), 4),
                },
            }
        )

    if scored:
        anchor_values = sorted(
            [
                max(
                    float((row.get("breakdown") or {}).get("anchor_match") or 0.0),
                    float(((row.get("breakdown") or {}).get("pattern_focus") or 0.0) * 0.9),
                )
                for row in scored
            ],
            reverse=True,
        )
        best_anchor = anchor_values[0] if anchor_values else 0.0
        # Adaptive gate: keep anchor grounding strong without collapsing to zero candidates.
        dynamic_anchor_threshold = max(0.02, min(anchor_threshold, best_anchor * 0.72))
        min_anchor_required = max(2, min(count, 3))
        anchor_filtered = [
            row
            for row in scored
            if max(
                float((row.get("breakdown") or {}).get("anchor_match") or 0.0),
                float(((row.get("breakdown") or {}).get("pattern_focus") or 0.0) * 0.9),
            )
            >= dynamic_anchor_threshold
        ]
        if len(anchor_filtered) >= min_anchor_required:
            rejected_by_anchor += max(0, len(scored) - len(anchor_filtered))
            scored = anchor_filtered
        else:
            dynamic_anchor_threshold = 0.0
    else:
        dynamic_anchor_threshold = 0.0

    scored.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    if not scored:
        # If strict visual/distinctness filters remove everything, fall back to conceptual internet matches.
        for c in ranked_candidates:
            text = float(text_scores.get(c.url, 0.35))
            llm = float(llm_scores.get(c.url, 0.4 if api_key else 0.2))
            candidate_text = f"{c.title} {_url_text_signature(c.url)}"
            anchor_match = _anchor_relevance_score(anchor_terms, candidate_text, text, llm)
            pattern_focus, color_resonance = _pattern_relevance_score(
                pattern_terms=pattern_terms,
                candidate_text=candidate_text,
                current_hist=current_hist,
                candidate_hist=None,
                text_score=text,
                llm_score=llm,
            )
            direction_id, direction_label, direction_match = _direction_match(
                directions=directions,
                candidate_text=candidate_text,
                text_score=text,
                llm_score=llm,
                pattern_focus=pattern_focus,
            )
            subject_overlap = _token_overlap_score(seed_subject_terms, candidate_text) if seed_subject_terms else 0.0
            if len(history_urls) == 0 and len(seed_subject_terms) >= 2 and subject_overlap < 0.08:
                continue
            novelty = 0.02 if c.url in seen else 1.0
            coarse_score = (text * 0.34) + (llm * 0.24) + (anchor_match * 0.18) + (pattern_focus * 0.18) + (novelty * 0.06)
            scored.append(
                {
                    "candidate": c,
                    "score": float(coarse_score),
                    "direction_id": direction_id,
                    "direction_label": direction_label,
                    "breakdown": {
                        "visual_proximity": 0.0,
                        "visual_similarity": 0.0,
                        "motif_resonance": 0.0,
                        "text_relevance": round(text, 4),
                        "llm_concept": round(llm, 4),
                        "anchor_match": round(anchor_match, 4),
                        "seed_subject_overlap": round(subject_overlap, 4),
                        "pattern_focus": round(pattern_focus, 4),
                        "color_resonance": round(color_resonance, 4),
                        "direction_match": round(direction_match, 4),
                        "concept_bridge": round((text * 0.45) + (llm * 0.55), 4),
                        "novelty": round(float(novelty), 4),
                        "taste": 0.5,
                        "feedback_pref": 0.5,
                        "leap_bonus": 0.0,
                    },
                }
            )
        scored.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    top_rows = scored[: max(count, 24)]

    selected_rows: list[tuple[dict[str, Any], str]] = []
    if count >= 3 and top_rows:
        # Stronger proposal policy: 2 anchor-strong + 1 associative leap.
        anchor_slot_floor = max(0.06, dynamic_anchor_threshold)
        used_urls: set[str] = set()

        # Direction stage: try to surface one option per planned direction.
        for d in directions[:3]:
            did = str((d or {}).get("id") or "").strip()
            if not did:
                continue
            direction_rows = [
                r
                for r in top_rows
                if str(r.get("direction_id") or "").strip() == did
                and float(((r.get("breakdown") or {}).get("direction_match") or 0.0)) >= 0.08
                and (
                    not first_hop
                    or float(((r.get("breakdown") or {}).get("seed_subject_overlap") or 0.0)) >= 0.08
                )
            ]
            if not direction_rows:
                direction_rows = [r for r in top_rows if str(r.get("direction_id") or "").strip() == did]
            direction_rows.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
            for row in direction_rows:
                cand = row.get("candidate")
                if not isinstance(cand, Candidate) or cand.url in used_urls:
                    continue
                selected_rows.append((row, "direction"))
                used_urls.add(cand.url)
                break

        by_anchor = sorted(
            [
                r
                for r in top_rows
                if max(
                    float((r.get("breakdown") or {}).get("anchor_match") or 0.0),
                    float(((r.get("breakdown") or {}).get("pattern_focus") or 0.0) * 0.9),
                )
                >= anchor_slot_floor
                and (
                    not first_hop
                    or float((r.get("breakdown") or {}).get("seed_subject_overlap") or 0.0) >= 0.08
                )
            ],
            key=lambda r: max(
                float((r.get("breakdown") or {}).get("anchor_match") or 0.0),
                float(((r.get("breakdown") or {}).get("pattern_focus") or 0.0) * 0.9),
            ),
            reverse=True,
        )
        by_leap = sorted(
            [
                r
                for r in top_rows
                if max(
                    float((r.get("breakdown") or {}).get("anchor_match") or 0.0),
                    float(((r.get("breakdown") or {}).get("pattern_focus") or 0.0) * 0.9),
                )
                >= (anchor_slot_floor * 0.6)
                and (
                    not first_hop
                    or float((r.get("breakdown") or {}).get("seed_subject_overlap") or 0.0) >= 0.08
                )
            ]
            or top_rows,
            key=lambda r: float(
                (((r.get("breakdown") or {}).get("leap_bonus") or 0.0) * 1.2)
                + (((r.get("breakdown") or {}).get("concept_bridge") or 0.0) * 1.0)
                + (((r.get("breakdown") or {}).get("anchor_match") or 0.0) * 0.45)
                + (((r.get("breakdown") or {}).get("pattern_focus") or 0.0) * 0.4)
                + (((r.get("breakdown") or {}).get("direction_match") or 0.0) * 0.3)
            ),
            reverse=True,
        )
        for row in by_anchor:
            cand = row.get("candidate")
            if not isinstance(cand, Candidate) or cand.url in used_urls:
                continue
            selected_rows.append((row, "anchor"))
            used_urls.add(cand.url)
            if len([1 for _r, role in selected_rows if role == "anchor"]) >= 2:
                break
        for row in by_leap:
            cand = row.get("candidate")
            if not isinstance(cand, Candidate) or cand.url in used_urls:
                continue
            bd = row.get("breakdown") or {}
            if (
                max(float(bd.get("anchor_match") or 0.0), float(bd.get("pattern_focus") or 0.0) * 0.9) < (anchor_slot_floor * 0.6)
                and float(bd.get("concept_bridge") or 0.0) < 0.28
            ):
                continue
            selected_rows.append((row, "leap"))
            used_urls.add(cand.url)
            break
        for row in top_rows:
            cand = row.get("candidate")
            if not isinstance(cand, Candidate) or cand.url in used_urls:
                continue
            if first_hop and float((row.get("breakdown") or {}).get("seed_subject_overlap") or 0.0) < 0.08:
                continue
            selected_rows.append((row, "wildcard"))
            used_urls.add(cand.url)
            if len(selected_rows) >= count:
                break
        selected_rows = selected_rows[:count]
    else:
        selected_rows = [(row, "top") for row in top_rows[:count]]

    out_candidates: list[dict[str, Any]] = []
    for row, selection_role in selected_rows:
        cand = row["candidate"]
        bd = row.get("breakdown") or {}
        sorted_reasons = sorted(bd.items(), key=lambda kv: float(kv[1]), reverse=True)
        reason_keys = [k for k, _v in sorted_reasons[:2]]
        reason_map = {
            "visual_proximity": "visual continuity",
            "visual_similarity": "visual relation",
            "motif_resonance": "motif resonance",
            "text_relevance": "text/theme relevance",
            "llm_concept": "conceptual association",
            "anchor_match": "seed-anchor match",
            "seed_subject_overlap": "seed subject overlap",
            "pattern_focus": "pattern continuity",
            "color_resonance": "color resonance",
            "direction_match": "associative direction match",
            "concept_bridge": "conceptual bridge",
            "novelty": "novelty",
            "taste": "learned taste fit",
            "feedback_pref": "your explicit preferences",
            "leap_bonus": "meaningful leap from seed",
        }
        reason = " + ".join(reason_map.get(k, k) for k in reason_keys if k in reason_map) or "overall best weighted score"
        out_candidates.append(
            {
                "url": cand.url,
                "title": cand.title,
                "source": cand.source,
                "score": round(float(row.get("score") or 0.0), 4),
                "reason": reason,
                "selection_role": selection_role,
                "direction_id": str(row.get("direction_id") or ""),
                "direction_label": str(row.get("direction_label") or ""),
                "breakdown": bd,
            }
        )
    return {
        "candidates": out_candidates,
        "query": query,
        "concepts": concepts[:6],
        "anchors": anchor_terms[:8],
        "patterns": pattern_terms[:10],
        "pattern_colors": color_terms[:4],
        "seed_subject_terms": seed_subject_terms[:8],
        "directions": directions[:3],
        "diagnostics": {
            "anchor_threshold": anchor_threshold,
            "dynamic_anchor_threshold": round(float(dynamic_anchor_threshold), 4),
            "total_ranked": len(ranked_candidates),
            "total_featured": len(features),
            "returned_count": len(out_candidates),
            "selection_policy": "direction_stage_then_anchor_leap" if count >= 3 else "top_score",
            "rejected_by_anchor": rejected_by_anchor,
            "rejected_by_seed_subject": rejected_by_seed_subject,
            "rejected_by_leap_band": rejected_by_leap_band,
            "rejected_by_distinct": rejected_by_distinct,
        },
    }


def record_association_winner_choice(
    current_url: str,
    chosen_url: str,
    rejected_urls: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rej = [str(x).strip() for x in (rejected_urls or []) if str(x).strip()]
    clean_context = dict(context or {})
    clean_context["mode"] = "pairwise_choice"
    cur = str(current_url or "").strip()
    chosen = str(chosen_url or "").strip()
    if not cur or not chosen:
        raise ValueError("current_url and chosen_url are required.")

    cur_raw = _fetch_bytes(cur, timeout=12)
    cur_vec, cur_kind = _image_embedding_from_bytes(cur_raw or b"")
    if not cur_vec or not cur_kind:
        cur_vec, cur_kind = [], ""

    def append_row(label: str, candidate: str, extra_ctx: dict[str, Any]) -> None:
        emb_type = ""
        delta: list[float] = []
        if cur_vec and cur_kind:
            cand_raw = _fetch_bytes(candidate, timeout=12)
            cand_vec, cand_kind = _image_embedding_from_bytes(cand_raw or b"")
            if cand_vec and cand_kind == cur_kind and len(cand_vec) == len(cur_vec):
                emb_type = cand_kind
                delta = _vec_normalize(_vec_sub(cand_vec, cur_vec))
        payload: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "label": label,
            "current_url": cur,
            "candidate_url": candidate,
            "embedding_type": emb_type,
            "delta": delta,
            "context": extra_ctx,
        }
        _append_jsonl(_FEEDBACK_LOG_PATH, payload)

    append_row("good", chosen, clean_context)
    for bad in rej:
        bad_ctx = dict(clean_context)
        bad_ctx["against"] = chosen
        append_row("bad", bad, bad_ctx)

    trained = train_associations_feedback_model()
    return {
        "ok": True,
        "logged": True,
        "trained": bool(trained),
        "rejected_count": len(rej),
        "feedback_count": int((trained or {}).get("feedback_count") or 0),
        "positive_count": int((trained or {}).get("positive_count") or 0),
        "negative_count": int((trained or {}).get("negative_count") or 0),
    }


def bootstrap_feedback_from_training(
    training_folder: str,
    negatives_per_step: int = 3,
    max_pairs: int = 2400,
) -> dict[str, Any]:
    folder = Path(training_folder).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Training folder not found: {folder}")
    files = _sorted_training_images(folder)
    if len(files) < 6:
        raise ValueError("Need at least 6 images to bootstrap feedback.")

    vectors: list[tuple[str, list[float], str]] = []
    embed_type = ""
    for p in files:
        try:
            raw = p.read_bytes()
        except Exception:
            continue
        vec, kind = _image_embedding_from_bytes(raw)
        if not vec:
            continue
        if not embed_type:
            embed_type = kind
        if kind != embed_type:
            continue
        url = f"file://{p}"
        vectors.append((p.name, vec, url))
    if len(vectors) < 6:
        raise ValueError("Could not build enough embeddings from training images.")

    rng = random.Random(42)
    idxs = list(range(len(vectors)))
    appended = 0
    for i in range(len(vectors) - 1):
        if appended >= max_pairs:
            break
        cur_name, cur_vec, cur_url = vectors[i]
        nxt_name, nxt_vec, nxt_url = vectors[i + 1]
        pos_delta = _vec_normalize(_vec_sub(nxt_vec, cur_vec))
        _append_jsonl(
            _FEEDBACK_LOG_PATH,
            {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "label": "good",
                "current_url": cur_url,
                "candidate_url": nxt_url,
                "embedding_type": embed_type,
                "delta": pos_delta,
                "context": {"source": "bootstrap_training", "current_name": cur_name, "candidate_name": nxt_name},
            },
        )
        appended += 1
        bad_choices = [j for j in idxs if j not in {i, i + 1} and abs(j - i) > 2]
        rng.shuffle(bad_choices)
        for j in bad_choices[: max(1, negatives_per_step)]:
            if appended >= max_pairs:
                break
            bad_name, bad_vec, bad_url = vectors[j]
            neg_delta = _vec_normalize(_vec_sub(bad_vec, cur_vec))
            _append_jsonl(
                _FEEDBACK_LOG_PATH,
                {
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "label": "bad",
                    "current_url": cur_url,
                    "candidate_url": bad_url,
                    "embedding_type": embed_type,
                    "delta": neg_delta,
                    "context": {"source": "bootstrap_training", "current_name": cur_name, "candidate_name": bad_name, "against_next": nxt_name},
                },
            )
            appended += 1
    trained = train_associations_feedback_model()
    return {
        "ok": True,
        "appended_pairs": appended,
        "trained": bool(trained),
        "embedding_type": embed_type,
        "feedback_count": int((trained or {}).get("feedback_count") or 0),
        "positive_count": int((trained or {}).get("positive_count") or 0),
        "negative_count": int((trained or {}).get("negative_count") or 0),
    }
