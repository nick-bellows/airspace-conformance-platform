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

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[messaging,storage,api]"


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

USER acp

# Overridden per service in compose and in the Kubernetes manifests.
CMD ["acp-feed", "--help"]
