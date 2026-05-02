#!/usr/bin/env bash
# Restore from an archive produced by backup_compositions.sh (or its extracted folder).
# Usage:
#   ./scripts/restore_compositions.sh /path/to/composition-backup-YYYYMMDD-HHMMSS.tar.gz
#   ./scripts/restore_compositions.sh /path/to/extracted/composition-backup-YYYYMMDD-HHMMSS/
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <backup.tar.gz OR extracted-backup-directory>" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$1"
WORKDIR=""

cleanup() {
  if [[ -n "$WORKDIR" && -d "$WORKDIR" ]]; then
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

if [[ -f "$SRC" ]]; then
  WORKDIR="$(mktemp -d)"
  tar -xzf "$SRC" -C "$WORKDIR"
  # Archive contains one directory composition-backup-*
  WORKDIR="$(find "$WORKDIR" -maxdepth 1 -type d -name 'composition-backup-*' | head -1)"
else
  WORKDIR="$SRC"
fi

if [[ ! -d "$WORKDIR" ]]; then
  echo "Could not find backup contents in $SRC" >&2
  exit 1
fi

cd "$ROOT"
if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
fi
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-djangoscrap.settings}"

echo "Restoring from: $WORKDIR"
read -r -p "This will merge/replace data via loaddata and extract files. Continue? [y/N] " ans
if [[ ! "${ans:-}" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

if [[ -f "$WORKDIR/media.tar.gz" ]]; then
  tar -xzf "$WORKDIR/media.tar.gz" -C "$ROOT"
fi
if [[ -f "$WORKDIR/composition_audio_sources.tar.gz" ]]; then
  tar -xzf "$WORKDIR/composition_audio_sources.tar.gz" -C "$ROOT"
fi
if [[ -f "$WORKDIR/static-composition_thumbnails.tar.gz" ]]; then
  mkdir -p "$ROOT/static"
  tar -xzf "$WORKDIR/static-composition_thumbnails.tar.gz" -C "$ROOT/static"
fi
if [[ -f "$WORKDIR/compositions-runtime.tar.gz" ]]; then
  tar -xzf "$WORKDIR/compositions-runtime.tar.gz" -C "$ROOT"
fi

if [[ -f "$WORKDIR/db.sqlite3.filecopy" ]]; then
  cp -a "$WORKDIR/db.sqlite3.filecopy" "${DB_SQLITE_PATH:-$ROOT/db.sqlite3}"
  echo "Restored SQLite file. You may skip loaddata if this is the full DB."
fi

if [[ -f "$WORKDIR/django-dumpdata.json" ]]; then
  python "$ROOT/manage.py" loaddata --skip-checks "$WORKDIR/django-dumpdata.json"
fi

if [[ -f "$WORKDIR/mysql-dump.sql" ]] && command -v mysql >/dev/null 2>&1; then
  echo "mysql-dump.sql found. Import manually if needed, e.g.:"
  echo "  mysql -h\$DB_HOST -u\$DB_USER -p \$DB_NAME < $WORKDIR/mysql-dump.sql"
fi

echo "Restore steps finished. Restart the app and verify compositions in the dashboard."
