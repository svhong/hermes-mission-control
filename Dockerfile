# Hermes Mission Control: one small image for the hub, the collector and the
# container monitor (pick with the command). Standard library only, no pip.
FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The hub runs as "hmc"; the collector is started as Hermes's user (compose sets
# `user:`) because only that user can read Hermes's files.
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin hmc \
 && mkdir -p /data && chown hmc:hmc /data

WORKDIR /app
COPY collector/ /app/collector/
COPY hub/ /app/hub/
COPY docker/healthcheck.py /app/docker/healthcheck.py

USER hmc
EXPOSE 8788
# Daily token ledger lives here (a volume in docker-compose.yml).
ENV STATE_DIRECTORY=/data
CMD ["python3", "/app/hub/hub.py"]
