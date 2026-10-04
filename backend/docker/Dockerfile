FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Non-root runtime user (kept early so subsequent COPYs are chowned in one pass).
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid ${APP_GID} app && useradd --uid ${APP_UID} --gid ${APP_GID} -m app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app

# Writable runtime storage for uploads/ops; owned by the non-root app user.
RUN mkdir -p /app/.local_uploads && chown -R ${APP_UID}:${APP_GID} /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

USER ${APP_UID}:${APP_GID}

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
