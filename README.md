# Composition Webapp

Last updated: April 2026

Django application for curating **image/video sources**, configuring **visual compositions** (layouts, filters, audio), and publishing **shareable public pages**. Background video rendering is handled asynchronously via **Celery** (MoviePy, FFmpeg, optional AWS S3).

## Requirements

- **Python** 3.10+ (3.10.17 is used in local dev)
- **Database**: SQLite works out-of-the-box for local dev; MySQL is supported via env config
- **Redis** (Celery broker/backend: `redis://localhost:6379/0`)
- **FFmpeg** on `PATH` (used by MoviePy and `ffmpeg` Python bindings)
- Optional: **AWS** credentials if `USE_S3_STORAGE=True`

## Quick start

```bash
cd composition_webapp-main
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

1. Copy `.env.example` to `.env` and set database credentials and `DJANGO_SECRET_KEY` (see [Configuration](#configuration)).
1. If using MySQL, create the database and user matching `.env`.  
   If using SQLite, this step is not required.
1. Run migrations and create a superuser:

```bash
python manage.py migrate
python manage.py createsuperuser
```

1. Start the app:

```bash
python manage.py runserver
```

1. Open the dashboard (after login): [http://127.0.0.1:8000/admin-dashboard/](http://127.0.0.1:8000/admin-dashboard/)

2. For **render/export tasks**, start Redis and a Celery worker (from project root, with the same `DJANGO_SETTINGS_MODULE`):

```bash
redis-server   # or your OS service
celery -A celery_app worker --loglevel=info
```

## Configuration

Environment variables are loaded from `.env` (see `.env.example`). Important settings in `djangoscrap/settings.py`:

| Variable | Purpose |
| ---------- | --------- |
| `DJANGO_SECRET_KEY` | Django secret; **set in production** |
| `DEBUG` | `True` / `False` |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | MySQL connection |
| `USE_S3_STORAGE` | `True` to store media on S3 (`django-storages` + `boto3`; install if not already present) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_REGION_NAME` | S3 when enabled |
| `DATA_UPLOAD_MAX_NUMBER_FILES`, `DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE` | Large batch uploads |
| `COMPOSITION_AUDIO_SOURCES_ROOT` | Absolute path to the on-disk audio library (defaults to `composition_audio_sources/` inside the project) |

**Security:** Do not commit real secrets. Replace any default passwords before deploying.

## Backing up compositions (critical)

Your compositions live in the **database** (metadata, source selections, URLs) and in **`media/`** (uploads, generated videos, thumbnails). Those paths are typically **not** in Git. A disk failure or OS reinstall without a backup **will** lose them.

**Do this on a schedule (e.g. weekly):**

1. From the project root, with `.env` configured and venv active if you use one:

   ```bash
   chmod +x scripts/backup_compositions.sh
   ./scripts/backup_compositions.sh
   ```

2. This writes `backups/composition-backup-YYYYMMDD-HHMMSS.tar.gz` containing a Django export, optional DB snapshot, and archives of `media/`, `composition_audio_sources/`, and related folders.

3. **Copy that `.tar.gz` off this computer** — cloud drive, external disk, NAS, or object storage. One local copy is not enough if the machine is lost or wiped.

**Optional:** Set `BACKUP_DIR` to write directly to an external volume, e.g. `BACKUP_DIR=/Volumes/YourDisk/composition-backups ./scripts/backup_compositions.sh`.

**Production:** Prefer **managed database backups** (RDS, etc.) and **`USE_S3_STORAGE=True`** so media survives server loss. The script remains useful for full portability and dev machines.

**Restore:** See `scripts/restore_compositions.sh` (use on a fresh clone after migrations).

## Cloudflare R2 backup and restore

Use `scripts/backup_r2.sh` to pull a full local copy of your R2 bucket.

### 1) Create Cloudflare R2 API credentials

1. Open Cloudflare Dashboard -> **R2**.
2. Go to **Manage R2 API Tokens** (or **API Tokens** section under R2).
3. Create a token with at least bucket read access for backups:
   - `Object Read` for backup-only
   - `Object Read + Write` if you also want restore uploads from this machine
4. Save the generated:
   - Access Key ID
   - Secret Access Key
5. Copy your R2 account ID from Cloudflare (used for endpoint URL).

### 2) Add environment variables to `.env`

Add:

```env
R2_ACCOUNT_ID=your_cloudflare_account_id
R2_BUCKET_NAME=your_bucket_name
AWS_ACCESS_KEY_ID=your_r2_access_key_id
AWS_SECRET_ACCESS_KEY=your_r2_secret_access_key
BACKUP_DIR=./backups/r2
```

`BACKUP_DIR` is optional; default is `./backups/r2`.

### 3) Run the backup script

From project root:

```bash
chmod +x scripts/backup_r2.sh
set -a && source .env && set +a
./scripts/backup_r2.sh
```

What it does:

- validates required env vars
- creates local backup directory if missing
- runs `aws s3 sync` from `s3://$R2_BUCKET_NAME` to local backup dir using your R2 endpoint
- does **not** delete remote files

### 4) Restore local backup back into R2

To push local backup files to R2:

```bash
set -a && source .env && set +a
aws s3 sync "${BACKUP_DIR:-./backups/r2}" "s3://${R2_BUCKET_NAME}" \
  --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
```

Notes:

- This uploads/updates objects in R2 from local backup.
- It does **not** remove extra files from R2 unless you explicitly add `--delete` (not recommended for first restore).
- Test restore on a staging bucket first when possible.

## Project layout

| Path | Role |
| ------ | ------ |
| `manage.py` | Django entrypoint |
| `djangoscrap/` | Main app: `settings.py`, `urls.py`, `views.py`, `models.py`, `templates/` |
| `djangoscrap/static/` | App static assets (admin UI, public site assets) |
| `static/` | Extra static dir on `STATICFILES_DIRS` (e.g. `composition_thumbnails/` for task output) |
| `composition_audio_sources/` | **Default local audio library** — each subfolder is an “Audio source” in the composition editor; Noise Studio saves to `composition_audio_sources/noise_studio/`. Override path with `COMPOSITION_AUDIO_SOURCES_ROOT` in `.env` if needed (e.g. legacy sibling folder). |
| `compositions/audios/` | Runtime path for uploaded composition audio (created by the app if missing) |
| `media/` | User uploads, generated video/audio (local when S3 disabled; see `.gitignore`) |
| `celery_app.py` | Celery app and long-running composition/video tasks |
| `archive/` | **Offline backups only** — old duplicates, temp exports, SQL dumps; not used at runtime |

**Git:** Root `.gitignore` excludes `.env`, `__pycache__`, local SQLite DBs, and typical user media under `media/`. Keep secrets and large binaries out of version control.

## Documentation map

| File | Purpose |
| ------ | --------- |
| `README.md` | Main setup + operations guide |
| `BIG_PICTURE_README.md` | Product/installation direction (gallery wall vision and constraints) |
| `INGESTION_CAMPAIGN_README.md` | Ingestion architecture + operating model |
| `INGESTION_IMPLEMENTATION_PLAN.md` | Ingestion execution checklist and roadmap |
| `NFT_LAUNCH_README.md` | The Feed launch: contract spec, mint UI, deploy checklist, weaknesses |
| `NFT_MEDIA_GENERATION_PLAN.md` | Capture pipeline, asset specs, source-signature, versioning, metadata schema, runbook |
| `PRODUCTION_DEPLOYMENT_PLAN.md` | Hosting, budget tiers, longevity, NFT contract strategy |
| `contracts/README.md` | Foundry workspace dev guide for `TheFeed.sol` |

## URLs (high level)

| Path | Description |
| ------ | ------------- |
| `/` | Redirects to `admin-dashboard` |
| `/login/`, `/logout/` | Auth |
| `/admin-dashboard/` | Main dashboard |
| `/composition-add/`, `/composition-edit/<id>/` | Create/edit compositions |
| `/source-library/`, `/new-source/` | Media source buckets |
| `/audio-sources/` | Audio libraries |
| `/composition/<id>/` | Composition detail (authenticated) |
| `/<slug>/` | **Public** composition page (`composition_public_page`) |
| `/character-thoughts/wall/` | Wall operator panel (9-screen orchestration controls) |
| `/wall/player/<screen_id>/` | Kiosk player endpoint per screen (`1..9`) |
| `/api/wall/state` | Per-screen timed assignment feed (`tick_id`, `start_at_unix_ms`) |
| `/api/wall/control` | Start/stop/next/pin/unpin orchestration actions |
| `/api/wall/heartbeat` | Player drift and health telemetry |

Public pages use the slug you configure when saving a composition.

## Composition types

Types are stored as strings on `Composition.type`. In the admin UI they include:

| Type | Summary |
| ------ | --------- |
| **Classic** | Background + foreground sources |
| **Single** | Single-source / morph-style use |
| **Tunnel** | Layered tunnel presentation |
| **Quad** | 2×2 grid |
| **Mash** | Layered patchwork grid (square tiles) |
| **Mash (fine)** | Same as Mash with **half-sized** square tiles (`mash-fine`) |
| **Vertical / Horizontal Stripes** | Stripe variants of the patchwork pipeline |
| **Psychedelic** | Background-driven effect |
| **Kaleidoscope** | Background-driven effect |
| **Dirty Scroll** | Scroll-style layout (legacy `social-scroll` may map here) |
| **Scrollhole** | Scroll variant |
| **Left to Right**, **Top and Bottom** | Split layouts |

Validation rules (e.g. which modes require background-only vs foreground) live in `djangoscrap/views.py` around composition create/update.

## Development notes

- **Filters / preview:** The composition editor includes client-side preview logic in templates such as `djangoscrap/templates/admin/composition.html`.
- **Public player:** `djangoscrap/templates/composition_public.html` implements layout modes matching the saved `type`.
- **Dependencies:** Video stack uses `moviepy`, `imageio`, `imageio-ffmpeg`, `pillow`, and FFmpeg. `playwright` is available for browser automation when needed.

## Repository hygiene

- Keep runtime code in `djangoscrap/`; move experiments, old dumps, and one-off artifacts into `archive/` if needed.
- Delete machine-specific noise (`._*`, `.DS_Store`, `__pycache__/`, `.pytest_cache/`) before sharing snapshots.
- Avoid storing secrets in docs or committed files; keep all credentials in `.env`.
- If unsure whether a file is still needed, archive first, then delete after one stable release cycle.

## Autonomous ingestion campaigns

The app includes a "set it and forget it" ingestion system that autonomously
sources images/videos for a folder concept and keeps that folder fresh over
time. Three-layer semantic dedupe (SHA256 / pHash / CLIP), thumbnail→full-res
URL rewriting, per-host polite throttling, HEAD-before-GET junk filtering,
and AI vision-QC are all active by default.

See [**`INGESTION_CAMPAIGN_README.md`**](./INGESTION_CAMPAIGN_README.md) for
architecture, operational commands, how to read a `campaign_last_report`,
and the active roadmap for social-media account archiving + multi-engine
fan-out.

## Organizing source folders

If your source library gets messy, use the built-in organizer command. It applies a subject-first naming logic:

- Canonical folder format: `subject__style__source`
- Example: `beach__tumblr-style__tumblr`
- Source examples normalized by the command: `insta -> instagram`, `google/yandex -> search`

Run a safe preview first:

```bash
python manage.py organize_sources
```

Then apply renames (also updates DB references in `Bucket` and every composition source list):

```bash
python manage.py organize_sources --apply
```

Recommended workflow:

1. Run dry-run and review proposed renames.
2. Backup your `composition_sources_unprocessed/` folder.
3. Run with `--apply`.
4. Open `/source-library/` and one or two compositions to confirm sources still resolve.

## License / ownership

Add your license and attribution here if this repo is distributed beyond your team.
