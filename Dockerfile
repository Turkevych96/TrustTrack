FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_DEBUG=False \
    DJANGO_STATIC_ROOT=/app/staticfiles \
    TRUSTTRACK_SQLITE_PATH=/data/db.sqlite3 \
    TRUSTTRACK_BACKUP_DIR=/data/backups

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
RUN chmod +x /app/scripts/docker-entrypoint.sh

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["python", "manage.py", "run_trusttrack", "--host", "0.0.0.0", "--port", "8000", "--site-runner", "gunicorn"]
