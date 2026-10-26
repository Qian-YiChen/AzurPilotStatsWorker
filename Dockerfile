# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM alpine:3 AS alas-source
ARG ALAS_REVISION=d0ce990caf24b821e4250909ada78016f95d31d0
# Fetch a pinned source archive, never repository history or local Git config.
ADD https://codeload.github.com/wess09/AzurPilot/tar.gz/${ALAS_REVISION} /source.tar.gz
RUN mkdir -p /source /alas/bin \
    && tar -xzf /source.tar.gz -C /source --strip-components=1 \
    && cp -a /source/module /source/assets /source/deploy /source/config /source/LICENSE /alas/ \
    && cp -a /source/bin/ocr_models /alas/bin/

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv
FROM python:3.14-slim-bookworm AS runtime
COPY --from=uv /uv /usr/local/bin/uv
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home worker \
    && mkdir /data && chown worker:worker /data
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra ocr --no-cache --python /usr/local/bin/python
COPY worker/ ./worker/
COPY docker/ ./docker/
COPY --from=alas-source --chown=worker:worker /alas /opt/alas
ARG ALAS_REVISION=d0ce990caf24b821e4250909ada78016f95d31d0
LABEL io.azurpilot.alas.revision=$ALAS_REVISION
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data ALAS_ROOT=/opt/alas ALAS_PYTHON=/app/.venv/bin/python \
    LISTEN_HOST=0.0.0.0 LISTEN_PORT=8091 \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
USER worker
EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('LISTEN_PORT','8091')+'/healthz',timeout=3).read()"
CMD ["python", "-m", "worker.app"]

FROM runtime AS release
RUN --network=none python docker/smoke.py
