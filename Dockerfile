# syntax=docker/dockerfile:1
# Image par défaut : légère, sans navigateur.
# Image avec Playwright (optionnelle) : docker compose --profile playwright ...
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/config/config.yaml DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin watcher \
 && mkdir -p /data /config && chown watcher:watcher /data
COPY watcher ./watcher
COPY tests/fixtures ./tests/fixtures
USER watcher
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "watcher"]
CMD ["run"]

FROM base AS playwright
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright==1.48.0 \
 && playwright install --with-deps chromium \
 && chmod -R a+rx /ms-playwright
USER watcher

FROM base AS default
