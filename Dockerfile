# One image, four entry points.
#
# The four services share a base layer and differ only by the command compose
# and Kubernetes give them. That keeps the build simple and the layer cache warm
# while leaving them independently deployable and independently scalable.
#
# The trade-off, stated so it is not mistaken for an oversight: every image
# carries every service's dependencies. Once the conformance service pulls in
# PyTorch at M3, the API image would carry it too, which is when this splits
# into per-service builds with an EXTRAS build argument.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Dependency metadata first, so a source-only change does not invalidate the
# dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src

# CPU-only torch. The default wheel pulls the entire CUDA runtime -- roughly two
# gigabytes of GPU libraries that would never be used on this stack.
#
# One install, with the PyTorch CPU index as an *extra* index rather than two
# sequential installs. Installing torch first and the project second worked, but
# only by luck: the second resolve would have been free to replace the CPU wheel
# with the CUDA one from PyPI the moment a version bump made the pinned range
# resolve differently, and a two-gigabyte image change is not something to
# discover in a release.
# `observability` is in the shipped image, not left to a sidecar. Without it
# every metric is a no-op and every span is dropped, so the Prometheus and
# Jaeger profiles in deploy/compose.yml would come up scraping a system that
# has nothing to say. The code still degrades if it is ever removed -- that is
# tested by a separate CI job -- but the default deployment is instrumented.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        ".[messaging,storage,api,ml,observability]"


FROM python:3.13-slim AS runtime

# Non-root from the start. Adding it later means discovering every place that
# assumed write access all at once.
RUN groupadd --system --gid 1001 acp \
    && useradd --system --uid 1001 --gid acp --home /app --shell /usr/sbin/nologin acp

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# Scenarios, migrations, and the Alembic config are runtime inputs, not build
# inputs, so they are copied rather than installed.
COPY --chown=acp:acp scenarios ./scenarios
COPY --chown=acp:acp migrations ./migrations
COPY --chown=acp:acp alembic.ini ./alembic.ini
# Trained residual models. Small, committed, and loaded at startup by the
# conformance service. Absent or corrupt, it falls back to dead reckoning and
# logs that it did -- see docs/cards/model-trajectory-predictor.md.
COPY --chown=acp:acp models ./models

USER acp

# Overridden per service in compose and in the Kubernetes manifests.
CMD ["acp-feed", "--help"]
