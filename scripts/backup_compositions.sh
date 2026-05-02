#!/usr/bin/env bash
# Create a portable archive of composition data: DB dump + Django JSON + media + audio library.
# Run regularly and copy the resulting .tar.gz OFF this machine (cloud drive, NAS, external disk).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST_ROOT="${BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$DEST_ROOT"
WORKDIR="$DEST_ROOT/composition-backup-$STAMP"
mkdir -p "$WORKDIR"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
fi
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-djangoscrap.settings}"

set -a
[[ -f "$ROOT/.env" ]] && . "$ROOT/.env"
set +a

echo "Writing backup to: $WORKDIR"

python "$ROOT/manage.py" dumpdata \
  --skip-checks \
  --natural-foreign --natural-primary \
  --exclude contenttypes \
  --exclude auth.permission \
  --exclude sessions \
  --indent 2 \
  -o "$WORKDIR/django-dumpdata.json"

DB_ENGINE="${DB_ENGINE:-mysql}"
if [[ "$DB_ENGINE" == "sqlite" ]]; then
  SQLITE_PATH="${DB_SQLITE_PATH:-$ROOT/db.sqlite3}"
  if [[ -f "$SQLITE_PATH" ]]; then
    cp -a "$SQLITE_PATH" "$WORKDIR/db.sqlite3.filecopy"
    echo "Included SQLite file copy: $SQLITE_PATH"
  fi
elif command -v mysqldump >/dev/null 2>&1 && [[ -n "${DB_USER:-}" ]]; then
  OUT_SQL="$WORKDIR/mysql-dump.sql"
  export MYSQL_PWD="${DB_PASSWORD:-}"
  if mysqldump \
    -h"${DB_HOST:-127.0.0.1}" \
    -P"${DB_PORT:-3306}" \
    -u"$DB_USER" \
    --single-transaction \
    --routines \
    --triggers \
    "${DB_NAME:-mydb}" >"$OUT_SQL" 2>/dev/null; then
    echo "Included mysqldump: $OUT_SQL"
  else
    echo "Warning: mysqldump failed (check .env DB_*). You still have django-dumpdata.json." >&2
    rm -f "$OUT_SQL" 2>/dev/null || true
  fi
  unset MYSQL_PWD
fi

AUDIO_ROOT="${COMPOSITION_AUDIO_SOURCES_ROOT:-$ROOT/composition_audio_sources}"
if [[ -d "$AUDIO_ROOT" ]]; then
  tar -czf "$WORKDIR/composition_audio_sources.tar.gz" -C "$(dirname "$AUDIO_ROOT")" "$(basename "$AUDIO_ROOT")"
fi

if [[ -d "$ROOT/media" ]]; then
  tar -czf "$WORKDIR/media.tar.gz" -C "$ROOT" media
fi

if [[ -d "$ROOT/static/composition_thumbnails" ]]; then
  tar -czf "$WORKDIR/static-composition_thumbnails.tar.gz" -C "$ROOT/static" composition_thumbnails
fi

if [[ -d "$ROOT/compositions" ]]; then
  tar -czf "$WORKDIR/compositions-runtime.tar.gz" -C "$ROOT" compositions
fi

{
  echo "{\"created\":\"$STAMP\",\"project_root\":\"$ROOT\",\"db_engine\":\"$DB_ENGINE\"}"
} >"$WORKDIR/manifest.json"

ARCHIVE="$DEST_ROOT/composition-backup-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$DEST_ROOT" "composition-backup-$STAMP"
rm -rf "$WORKDIR"

echo ""
echo "Done. Archive: $ARCHIVE"
echo "Copy this file to another disk or cloud (Time Machine, Dropbox, S3, etc.)."
echo "Without an off-machine copy, a lost disk still means lost data."
