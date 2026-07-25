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
# Every check runs, then every exit code is asserted at the end.
#
# Two traps this shape exists to avoid, both of which shipped a passing build
# from a failing gate:
#   * `&&`-chaining the two pytest runs let a failure in the first skip the
#     other ~818 tests entirely, so one red computation test hid the suite.
#   * `RUN` uses `/bin/sh -c`, which does NOT set -e, so mixing `&&` with `;`
#     splits this into independent lists: a failing ruff short-circuited its
#     own chain but execution continued into pytest, and because only the
#     LAST list decides the RUN exit status, green tests produced the marker
#     and a successful build despite lint errors.
# So: no `&&` between steps, capture each status, and gate on all of them.
#
# The two pytest invocations stay separate on purpose -- test_computation_service.py
# spawns real child interpreters and binds the computation runtime, which must
# not leak into the rest of the suite.
RUN pip install '.[dev]' || exit 1

# Chromium for the browser regressions, as its own layer so it caches across
# source changes. This lands in the `test` stage only -- `runtime` derives from
# `base`, so none of it reaches the production image.
RUN playwright install --with-deps chromium || exit 1

# The browser run uses `python -m pytest`, not bare `pytest`: only the module
# form puts the working directory on sys.path, and tests/browser imports its
# harness as `tests.browser.harness`. Bare pytest fails collection with
# ModuleNotFoundError and exits 2 before running anything.
RUN ruff check app tests; lint=$?; \
    ruff format --check app tests; fmt=$?; \
    pytest -q tests/test_computation_service.py; sidecar=$?; \
    pytest -q tests --ignore=tests/test_computation_service.py --ignore=tests/browser; rest=$?; \
    python -m pytest -q tests/browser; browser=$?; \
    echo "gate exit codes: lint=$lint fmt=$fmt sidecar=$sidecar rest=$rest browser=$browser"; \
    [ "$lint" -eq 0 ] && [ "$fmt" -eq 0 ] \
      && [ "$sidecar" -eq 0 ] && [ "$rest" -eq 0 ] && [ "$browser" -eq 0 ] \
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
