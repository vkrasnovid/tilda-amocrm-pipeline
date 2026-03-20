# Stage 1 — builder
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2 — runtime
FROM python:3.11-slim

COPY --from=builder /install /usr/local

WORKDIR /app

COPY ./app /app/app
COPY ./templates /app/templates
COPY ./alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Install gosu for privilege drop in entrypoint, then create appuser.
# The entrypoint runs as root, creates /data with correct ownership, then execs as appuser.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --no-create-home appuser \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
# CMD is overridden per-service in docker-compose.yml
