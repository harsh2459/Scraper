# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS frontend
WORKDIR /src/shared-scraper/tm_manager/frontend
COPY shared-scraper/tm_manager/frontend/package*.json ./
RUN npm ci
COPY shared-scraper/tm_manager/frontend/ ./
COPY shared-scraper/ops_frontend/ /src/shared-scraper/ops_frontend/
RUN npm run build && npm run build:admin

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/tmtracker
COPY shared-scraper/requirements.txt /tmp/scraper-requirements.txt
COPY shared-scraper/tm_manager/requirements.txt /tmp/user-requirements.txt
RUN pip install --no-cache-dir -r /tmp/scraper-requirements.txt -r /tmp/user-requirements.txt \
    && groupadd --gid 10001 tmtracker && useradd --uid 10001 --gid 10001 --no-create-home tmtracker
COPY shared-scraper/*.py ./
COPY shared-scraper/control_plane/ ./control_plane/
COPY shared-scraper/migrations/ ./migrations/
COPY shared-scraper/tm_manager/*.py ./tm_manager/
COPY shared-scraper/tm_manager/templates/ ./tm_manager/templates/
COPY shared-scraper/tm_manager/scripts/ ./tm_manager/scripts/
COPY --from=frontend /src/shared-scraper/tm_manager/frontend/dist/ ./tm_manager/frontend/dist/
COPY --from=frontend /src/shared-scraper/admin_frontend/dist/ ./admin_frontend/dist/
COPY deploy/containers/staging_entrypoint.py /usr/local/bin/tmtracker-staging.py
COPY deploy/containers/production_entrypoint.py /usr/local/bin/tmtracker-production.py
COPY deploy/containers/drive_bootstrap.py /usr/local/bin/tmtracker-drive-bootstrap.py
USER 10001:10001
ENTRYPOINT ["python", "/usr/local/bin/tmtracker-staging.py"]
