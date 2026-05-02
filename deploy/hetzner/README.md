# Hetzner Production Deployment

This folder contains an idempotent deployment setup for Ubuntu VPS with:

- Gunicorn + Nginx
- systemd service units
- Redis + Celery worker
- Cloudflare R2-backed static/media storage

## Required local environment variables

Set these before running scripts:

- `SSH_KEY_PATH` (private key path)
- `SSH_USER`
- `SSH_HOST`
- `DOMAIN`

Optional:

- `SSH_PORT` (default `22`)
- `APP_DIR` (default `/srv/composition_webapp-main`)
- `APP_SERVICE_NAME` (default `composition-webapp`)
- `APP_USER` (default `SSH_USER`)
- `GUNICORN_WORKERS` (default `2`)
- `CELERY_CONCURRENCY` (default `1`)
- `RUN_CELERY` (default `true`; set `false` for a web-only public MVP)

## Required remote env file

Create remote file at `djangoscrap/.env` inside `APP_DIR` (or set `ENV_FILE` override in deploy script) using `deploy/hetzner/.env.production.example` as a template.

Important values:

- `DEBUG=False`
- `ALLOWED_HOSTS=...`
- `CSRF_TRUSTED_ORIGINS=...`
- DB credentials
- `REDIS_URL`
- `USE_R2_STORAGE=True`
- `R2_ACCOUNT_ID`
- `R2_BUCKET_NAME`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_PUBLIC_BASE_URL`

## Commands

Run a local preflight:

```bash
DOMAIN=edfornieles.com bash scripts/prod/mvp_preflight.sh
```

Prepare Cloudflare DNS/R2:

```bash
CLOUDFLARE_API_TOKEN=... \
CLOUDFLARE_ACCOUNT_ID=... \
DOMAIN=edfornieles.com \
SERVER_IPV4=1.2.3.4 \
R2_BUCKET_NAME=edfornieles-composition-prod \
bash scripts/prod/prepare_cloudflare.sh
```

Provision/reuse Hetzner server:

```bash
HETZNER_API_TOKEN=... \
HETZNER_SERVER_NAME=composition-prod \
HETZNER_LOCATION=nbg1 \
HETZNER_SERVER_TYPE=cpx21 \
HETZNER_IMAGE=ubuntu-24.04 \
SSH_PUBLIC_KEY_PATH=~/.ssh/id_ed25519.pub \
bash scripts/prod/provision_hetzner.sh
```

Deploy/update:

```bash
SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 DOMAIN=edfornieles.com \
bash scripts/prod/deploy_hetzner.sh
```

Upload a completed production env file before deploying:

```bash
SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 \
LOCAL_ENV_FILE=/path/to/production.env \
bash scripts/prod/upload_remote_env.sh
```

Restart services:

```bash
SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 \
bash scripts/prod/restart_hetzner.sh
```

Tail logs:

```bash
SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 SERVICE=all \
bash scripts/prod/logs_hetzner.sh
```

Smoke-check production:

```bash
DOMAIN=edfornieles.com bash scripts/prod/smoke_check.sh
```

Publish local MVP edits/content to production:

```bash
SSH_KEY_PATH=~/.ssh/id_ed25519 SSH_USER=root SSH_HOST=1.2.3.4 \
SYNC_R2=true R2_BUCKET_NAME=edfornieles-composition-prod \
bash scripts/prod/publish_local_mvp.sh
```

Export Route 53 DNS/registration state before switching to Cloudflare:

```bash
DOMAIN=edfornieles.com bash scripts/prod/route53_to_cloudflare.sh
```

After Cloudflare gives you two nameservers, prepare or apply the registrar update:

```bash
DOMAIN=edfornieles.com \
CLOUDFLARE_NAMESERVERS=ada.ns.cloudflare.com,bob.ns.cloudflare.com \
APPLY_NAMESERVERS=true \
bash scripts/prod/route53_to_cloudflare.sh
```

Create a read-only AWS shutdown inventory:

```bash
bash scripts/prod/aws_shutdown_inventory.sh
```
