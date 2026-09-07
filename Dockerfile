# syntax=docker/dockerfile:1

FROM node:24.18.0-bookworm-slim AS web-build

WORKDIR /workspace
COPY package.json package-lock.json .npmrc ./
COPY apps/web/package.json apps/web/package.json
RUN npm install --global npm@12.0.2
RUN npm ci --ignore-scripts
COPY apps/web apps/web
RUN npm run build

FROM python:3.14.6-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MARKET_MONITOR_ENV=release \
    MARKET_MONITOR_STATIC_ROOT=/opt/market-monitor/apps/web/dist \
    PYTHONPATH=/opt/market-monitor/apps/api/src:/opt/market-monitor/packages/analysis/src:/opt/market-monitor/packages/contracts/src:/opt/market-monitor/packages/data/src:/opt/market-monitor/packages/notifications/src:/opt/market-monitor/packages/persistence/src

WORKDIR /opt/market-monitor
RUN groupadd --gid 10001 marketmonitor \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /nonexistent --shell /usr/sbin/nologin marketmonitor \
    && mkdir -p /var/lib/market-monitor /tmp /run \
    && chown -R marketmonitor:marketmonitor /var/lib/market-monitor /tmp /run

COPY requirements-dev.lock ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements-dev.lock
COPY alembic.ini ./
COPY migrations migrations
COPY packages packages
COPY apps/api/src apps/api/src
COPY scripts/__init__.py scripts/official_runner.py scripts/production_worker.py scripts/run_local.py scripts/tdx_runner.py scripts/
COPY --from=web-build --chown=marketmonitor:marketmonitor /workspace/apps/web/dist apps/web/dist

USER marketmonitor
ENTRYPOINT ["python", "scripts/run_local.py"]
