# Qtracker hosted bridge (SPEC.md v4) for Google Cloud Run.
# Build context: the repository root (see cloudbuild.yaml); only server/ and deploy/hosted/ are uploaded.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ASKS_MODE=hosted

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
COPY deploy/hosted/requirements.txt deploy/hosted/requirements.txt
RUN pip install -r deploy/hosted/requirements.txt

COPY server/ server/
RUN rm -rf server/tests && find server -name __pycache__ -prune -exec rm -rf {} + \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin qtracker
USER qtracker

# Cloud Run sets $PORT. No access log: Cloud Run logs every request itself, and uvicorn's lines would repeat
# URLs (with OAuth state/code parameters) a second time.
CMD exec uvicorn server.app:app --host 0.0.0.0 --port "${PORT:-8080}" --no-access-log --timeout-graceful-shutdown 8
