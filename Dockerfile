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
COPY Dockerfile Dockerfile.corpus Dockerfile.compute docker-compose.computation.yml uv.lock package.json package-lock.json ./
COPY deploy ./deploy
# test_computation_service.py runs in its own pytest session on purpose: it
# spawns real child interpreters and binds the computation runtime, which must
# not leak into the rest of the suite. Keep the two invocations, but do NOT
# chain them with `&&` -- that made a failure in the first one skip the other
# ~818 tests entirely, so a red computation test hid the whole suite.
RUN pip install '.[dev]' \
    && ruff check app tests \
    && ruff format --check app tests \
    && set +e; \
       pytest -q tests/test_computation_service.py; sidecar=$?; \
       pytest -q tests --ignore=tests/test_computation_service.py; rest=$?; \
       echo "test exit codes: sidecar=$sidecar rest=$rest"; \
       [ "$sidecar" -eq 0 ] && [ "$rest" -eq 0 ] \
    && touch /app/.tests-passed

FROM base AS runtime

# BuildKit only builds stages the target depends on, and `runtime` derives from
# `base` -- so a plain `docker build` skipped the `test` stage and shipped
# without ever running ruff or pytest. Copying this marker makes `test` a real
# build dependency of `runtime` while keeping dev dependencies out of the
# runtime image (the alternative, `FROM test AS runtime`, would ship pytest and
# ruff to production).
COPY --from=test /app/.tests-passed /app/.tests-passed

USER assessment-ai

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
