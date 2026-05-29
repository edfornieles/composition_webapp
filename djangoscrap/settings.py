import os
from pathlib import Path
import pymysql
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Base directory (defined before load_dotenv so we can load <project>/.env reliably)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
# Also load app-local env file used in this workspace (`djangoscrap/.env`).
# Existing environment variables remain authoritative and are not overridden.
load_dotenv(BASE_DIR / "djangoscrap" / ".env", override=False)


def env_bool(name, default=False):
    return (os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"})


def env_csv(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def require_env(name):
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ImproperlyConfigured(f"Missing required production environment variable: {name}")
    return value

# Static and Media Files
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Local on-disk audio library: each subfolder is a selectable "Audio source" in compositions.
# Default lives inside the project so Noise Studio exports are easy to find and use immediately.
# Override with absolute path if you keep libraries elsewhere (e.g. legacy ../composition_audio_sources).
LOCAL_SOURCES_ROOT = Path(
    os.getenv("LOCAL_SOURCES_ROOT", str(BASE_DIR.parent / "composition_sources_unprocessed"))
).resolve()

COMPOSITION_AUDIO_SOURCES_ROOT = Path(
    os.getenv("COMPOSITION_AUDIO_SOURCES_ROOT", str(BASE_DIR / "composition_audio_sources"))
).resolve()

# Associations chains are generated runtime archives, not user source-library
# folders. Keep them out of composition_sources_unprocessed so Source Library
# stays curated input material.
ASSOCIATIONS_CHAINS_ROOT = Path(
    os.getenv("ASSOCIATIONS_CHAINS_ROOT", str(BASE_DIR / "associations" / "chains"))
).resolve()

# Large ingestion uploads (many files per batch).
# Keep configurable via env for production tuning.
DATA_UPLOAD_MAX_NUMBER_FILES = int(os.getenv("DATA_UPLOAD_MAX_NUMBER_FILES", "5000"))
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("DATA_UPLOAD_MAX_MEMORY_SIZE", str(50 * 1024 * 1024)))
FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("FILE_UPLOAD_MAX_MEMORY_SIZE", str(10 * 1024 * 1024)))

# Public site base for ingestion campaigns (no trailing slash), e.g. https://example.com
# When set, Yandex similar-image search can use your /source-media/… URLs so scrapes track the
# art already in each folder. Local-only URLs (http://127.0.0.1) cannot be fetched by Yandex.
INGESTION_CAMPAIGN_PUBLIC_BASE_URL = (os.getenv("INGESTION_CAMPAIGN_PUBLIC_BASE_URL") or "").strip().rstrip("/")

# Optional yt-dlp cookies file (Netscape format) for hosts that require login — notably Instagram
# in 2026, which returns 0 posts for public profile scrapes without session cookies. Export from
# a logged-in browser with an extension like "Get cookies.txt" and point this at the file.
INSTAGRAM_COOKIES_FILE = (os.getenv("INSTAGRAM_COOKIES_FILE") or "").strip()

# Same Netscape format — reuse a browser session after you solve Yandex SmartCaptcha once.
# Used by Yandex HTML fetch, Playwright dynamic extractor, and yt-dlp when the URL host is Yandex.
YANDEX_COOKIES_FILE = (os.getenv("YANDEX_COOKIES_FILE") or "").strip()

# OpenAI (optional — used by Character thoughts → “AI: deep research & update sources”)
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_RESEARCH_MODEL = (os.getenv("OPENAI_RESEARCH_MODEL") or "gpt-4o-mini").strip()
# Ethereum NFT minting. Keep contract details configurable so local dev can
# prepare NFT packages without a deployed contract.
NFT_ETH_CHAIN_ID = int(os.getenv("NFT_ETH_CHAIN_ID", "1"))
NFT_ETH_NETWORK_NAME = (os.getenv("NFT_ETH_NETWORK_NAME") or "Ethereum").strip()
NFT_ETH_CONTRACT_ADDRESS = (os.getenv("NFT_ETH_CONTRACT_ADDRESS") or "").strip()
NFT_ETH_MARKETPLACE_BASE_URL = (os.getenv("NFT_ETH_MARKETPLACE_BASE_URL") or "https://opensea.io/assets/ethereum").strip().rstrip("/")
NFT_STORAGE_GUIDANCE_URL = (os.getenv("NFT_STORAGE_GUIDANCE_URL") or "").strip()
# Stream-of-consciousness line generation (public thought pages)
MONOLOGUE_STREAM_MODEL = (os.getenv("MONOLOGUE_STREAM_MODEL") or "gpt-4o-mini").strip()

# External-drive corpus root for imageboard scrapes/profiles/models.
# Default is /Volumes/oom/imageboard_corpora (the user's external drive).
# Falls back to repo-local path only if the external drive isn't mounted.
IMAGEBOARD_CORPUS_ROOT = os.environ.get(
    "IMAGEBOARD_CORPUS_ROOT",
    "/Volumes/oom/imageboard_corpora",
)

# Imageboard /fit/ corpus-grounded persona.
# When True, personas matching FIT_PERSONA_SLUGS will return empty (no-op tick)
# rather than fall back to a generic OpenAI roleplay if the corpus cannot
# supply the minimum number of grounded fragments. This is intentional —
# the persona must speak from the corpus or not at all.
FIT_THOUGHTS_REQUIRE_GROUNDING = os.getenv("FIT_THOUGHTS_REQUIRE_GROUNDING", "1") not in ("0", "false", "False", "")
FIT_CORPUS_SOURCE_KEY = (os.getenv("FIT_CORPUS_SOURCE_KEY") or "fourchan_fit").strip()
FIT_PERSONA_SLUGS = tuple(
    s.strip().lower() for s in (os.getenv("FIT_PERSONA_SLUGS") or "fit,fourchan_fit_body_discipline,fourchan_fit_be_me_body_discipline").split(",") if s.strip()
)

# Art-mode toggles for /fit/ persona output
FIT_ART_MODE = os.getenv("FIT_ART_MODE", "1") not in ("0", "false", "False", "")
FIT_CONTAMINATION_INTENSITY = float(os.getenv("FIT_CONTAMINATION_INTENSITY", "0.75"))
FIT_ALLOW_GREENTEXT = os.getenv("FIT_ALLOW_GREENTEXT", "1") not in ("0", "false", "False", "")
FIT_ALLOW_BE_ME = os.getenv("FIT_ALLOW_BE_ME", "1") not in ("0", "false", "False", "")
FIT_BLOCK_RAW_SLURS = os.getenv("FIT_BLOCK_RAW_SLURS", "1") not in ("0", "false", "False", "")
FIT_BLOCK_ACTIONABLE_HARM = os.getenv("FIT_BLOCK_ACTIONABLE_HARM", "1") not in ("0", "false", "False", "")

# Local model runtime selection
FIT_LOCAL_RUNTIME = (os.getenv("FIT_LOCAL_RUNTIME") or "mlx").strip().lower()
FIT_MLX_BASE_MODEL = (os.getenv("FIT_MLX_BASE_MODEL") or "mlx-community/Qwen2.5-1.5B-Instruct-4bit").strip()
FIT_MLX_ADAPTER_DIR = (os.getenv("FIT_MLX_ADAPTER_DIR") or "/Volumes/oom/imageboard_corpora/fourchan_fit/models/fit_be_me_lora_1_5b").strip()
FIT_OLLAMA_MODEL = (os.getenv("FIT_OLLAMA_MODEL") or "fit-be-me").strip()
FIT_OLLAMA_HOST = (os.getenv("FIT_OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
FIT_LLAMACPP_MODEL_PATH = (os.getenv("FIT_LLAMACPP_MODEL_PATH") or "").strip()

# 8kun is intentionally disabled. Set ENABLE_8KUN_INGESTION=1 and EIGHTKUN_HOST
# to enable; the adapter is a stub today and will not run without explicit code.
ENABLE_8KUN_INGESTION = os.getenv("ENABLE_8KUN_INGESTION", "0") in ("1", "true", "True")
EIGHTKUN_HOST = (os.getenv("EIGHTKUN_HOST") or "").strip()

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "monologue-cache",
    }
}

# Security
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "your-default-secret-key")
DEBUG = env_bool("DEBUG", True)
LOGIN_URL = "/login/"
ALLOWED_HOSTS = env_csv("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_csv("CSRF_TRUSTED_ORIGINS")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)

if not DEBUG:
    if SECRET_KEY == "your-default-secret-key":
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set for production.")
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("ALLOWED_HOSTS must be set for production.")


# Installed Apps
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "djangoscrap",
]
# Middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files efficiently under gunicorn. Must come
    # right after SecurityMiddleware. Harmless under `manage.py runserver`
    # which retains its built-in static-file handling.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# URL Configuration
ROOT_URLCONF = "djangoscrap.urls"

# Templates
# Use the cached loader only when DEBUG=False (production). In DEBUG mode the
# raw filesystem/app_directories loaders re-read templates on every render, so
# template edits show up immediately without restarting gunicorn. Forcing the
# cached loader in dev was eating every CSS/JS edit until a manual restart.
_TEMPLATE_LOADERS = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]
if not DEBUG:
    _TEMPLATE_LOADERS = [
        (
            "django.template.loaders.cached.Loader",
            _TEMPLATE_LOADERS,
        ),
    ]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "OPTIONS": {
            "loaders": _TEMPLATE_LOADERS,
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# WSGI Application
WSGI_APPLICATION = "djangoscrap.wsgi.application"

# Optional AWS S3 Configuration (disabled by default for local mode)
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "us-east-1")
AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME")

USE_S3_STORAGE = env_bool("USE_S3_STORAGE", False)
USE_R2_STORAGE = env_bool("USE_R2_STORAGE", False)
R2_ACCOUNT_ID = (os.getenv("R2_ACCOUNT_ID") or "").strip()
R2_BUCKET_NAME = (os.getenv("R2_BUCKET_NAME") or "").strip()
R2_PUBLIC_BASE_URL = (os.getenv("R2_PUBLIC_BASE_URL") or "").strip().rstrip("/")
R2_ENDPOINT_URL = (
    (os.getenv("R2_ENDPOINT_URL") or "").strip()
    or (f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else "")
)

if USE_S3_STORAGE and USE_R2_STORAGE:
    raise ImproperlyConfigured("Set only one of USE_S3_STORAGE or USE_R2_STORAGE.")

if not DEBUG and USE_S3_STORAGE:
    for required_name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_STORAGE_BUCKET_NAME"):
        require_env(required_name)

if not DEBUG and USE_R2_STORAGE:
    for required_name in (
        "R2_ACCOUNT_ID",
        "R2_BUCKET_NAME",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_PUBLIC_BASE_URL",
    ):
        require_env(required_name)

if (USE_S3_STORAGE and AWS_STORAGE_BUCKET_NAME) or (USE_R2_STORAGE and R2_BUCKET_NAME):
    if "storages" not in INSTALLED_APPS:
        INSTALLED_APPS.append("storages")

if USE_S3_STORAGE and AWS_STORAGE_BUCKET_NAME:
    AWS_S3_CUSTOM_DOMAIN = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com"
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
    MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/"

if USE_R2_STORAGE and R2_BUCKET_NAME and R2_ENDPOINT_URL:
    AWS_ACCESS_KEY_ID = (os.getenv("R2_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    AWS_SECRET_ACCESS_KEY = (os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    AWS_STORAGE_BUCKET_NAME = R2_BUCKET_NAME
    AWS_S3_REGION_NAME = "auto"
    AWS_S3_ENDPOINT_URL = R2_ENDPOINT_URL
    AWS_S3_SIGNATURE_VERSION = "s3v4"
    AWS_DEFAULT_ACL = None
    AWS_S3_FILE_OVERWRITE = False
    AWS_QUERYSTRING_AUTH = False
    STORAGES = {
        "default": {"BACKEND": "djangoscrap.storage_backends.MediaR2Storage"},
        "staticfiles": {"BACKEND": "djangoscrap.storage_backends.StaticR2Storage"},
    }
    public_base = R2_PUBLIC_BASE_URL or f"{R2_ENDPOINT_URL}/{R2_BUCKET_NAME}"
    STATIC_URL = f"{public_base}/static/"
    MEDIA_URL = f"{public_base}/media/"

# Database Configuration
# DB_ENGINE choices:
# - mysql (default): uses DB_NAME/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT
# - sqlite: uses DB_SQLITE_PATH or <project>/db.sqlite3
DB_ENGINE = (os.getenv("DB_ENGINE") or "mysql").strip().lower()
if DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("DB_SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    pymysql.install_as_MySQLdb()
    if not DEBUG:
        for required_name in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST"):
            require_env(required_name)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("DB_NAME", "mydb"),
            "USER": os.getenv("DB_USER", "imguser"),
            "PASSWORD": os.getenv("DB_PASSWORD", "Welcome@1"),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "OPTIONS": {
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }

# Default Primary Key Field Type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Authentication
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Localization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Login and Logout Redirects
LOGIN_REDIRECT_URL = "/admin-dashboard/"
LOGOUT_REDIRECT_URL = "/login/"

# celery.py or Django settings

CELERY_TASK_ACKS_LATE = True
CELERYD_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 600  # Max 10 min
CELERY_TASK_SOFT_TIME_LIMIT = 500  # Warn at 8+ min
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_URL = (os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0").strip()
CELERY_RESULT_BACKEND = (os.getenv("CELERY_RESULT_BACKEND") or CELERY_BROKER_URL).strip()






