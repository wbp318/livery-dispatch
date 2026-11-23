# Production image for the dispatch app (v1.1.21).
#
#   docker build -t livery-dispatch .
#
# Fly.io deploys it with fly.toml (runbook: DEPLOY.md).
# tests/test_deploy_config.py pins
# the Dockerfile <-> fly.toml invariants.
#
# The container starts as root only so boot.py can give the /data volume
# (Fly mounts it root-owned) to the unprivileged `dispatch` user. boot.py
# then drops to that user, runs `alembic upgrade head`, and execs the CMD.
# ONE uvicorn worker on purpose: the rate-limit buckets, the /osrm cache
# and SQLite all assume a single process.

FROM python:3.11-slim-bookworm

# The image is the production artifact, so production mode is the default:
# without the required secrets it refuses to start (app.py boot guard).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPATCH_ENV=production \
    DATABASE_URL=sqlite:////data/dispatch.db

# sqlite3 CLI: DEPLOY.md's backup and inspection commands use it.
# tzdata: quiet hours resolve America/New_York through zoneinfo.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin dispatch \
 && mkdir -p /data \
 && chown dispatch:dispatch /data

WORKDIR /app

# Runtime deps only: requirements.txt adds pytest / bandit / pip-audit.
COPY requirements-runtime.txt .
RUN pip install -r requirements-runtime.txt

# App code stays root-owned and read-only to the app user. .dockerignore
# keeps tests, local databases, .env files and the OSRM build out.
COPY . .

EXPOSE 8080
ENTRYPOINT ["python", "boot.py"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
