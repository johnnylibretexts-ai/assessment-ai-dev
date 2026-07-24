FROM node:22.23.0-alpine3.23@sha256:35e2f96595091599e7c1fb0b61049e17d8478997b2aa13db51ad7995299fe55a AS mathjax-assets

WORKDIR /build

COPY package.json package-lock.json ./
COPY scripts/vendor-mathjax.mjs ./scripts/vendor-mathjax.mjs
RUN npm ci --ignore-scripts

ENV MATHJAX_VENDOR_OUTPUT=/vendor/mathjax
RUN npm run vendor:mathjax

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 assessment-ai \
    && useradd --uid 10001 --gid 10001 --create-home assessment-ai \
    && mkdir -p /data \
    && chown -R assessment-ai:assessment-ai /app /data

COPY pyproject.toml README.md ./
COPY app ./app
COPY --from=mathjax-assets /vendor/mathjax ./app/static/vendor/mathjax
COPY evaluation ./evaluation
RUN pip install .

FROM base AS test

COPY tests ./tests
COPY Dockerfile Dockerfile.compute docker-compose.computation.yml uv.lock package.json package-lock.json ./
COPY deploy ./deploy
RUN pip install '.[dev]' \
    && ruff check app tests \
    && ruff format --check app tests \
    && pytest -q tests/test_computation_service.py \
    && pytest -q tests --ignore=tests/test_computation_service.py

FROM base AS runtime

USER assessment-ai

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
