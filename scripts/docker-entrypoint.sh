#!/usr/bin/env sh
set -eu

: "${TRUSTTRACK_SQLITE_PATH:=/data/db.sqlite3}"
: "${TRUSTTRACK_BACKUP_DIR:=/data/backups}"
: "${DJANGO_STATIC_ROOT:=/app/staticfiles}"

mkdir -p "$(dirname "$TRUSTTRACK_SQLITE_PATH")" "$TRUSTTRACK_BACKUP_DIR" "$DJANGO_STATIC_ROOT"

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"
