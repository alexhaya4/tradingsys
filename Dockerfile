# Two stages: dependencies are installed into a virtualenv in the builder, then copied
# into a slim runtime image that carries no build toolchain and no uv.

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are resolved before the source is copied so that editing code does not
# invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

# tini reaps zombies and forwards SIGTERM, which is what makes graceful shutdown work
# under compose and Kubernetes.
RUN apt-get update \
    && apt-get install --no-install-recommends -y tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 tradingsys

WORKDIR /app

COPY --from=builder --chown=tradingsys:tradingsys /app/.venv /app/.venv
COPY --chown=tradingsys:tradingsys src ./src
COPY --chown=tradingsys:tradingsys config ./config
COPY --chown=tradingsys:tradingsys migrations ./migrations
COPY --chown=tradingsys:tradingsys alembic.ini pyproject.toml README.md ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRADINGSYS_CONFIG_DIR=/app/config

USER tradingsys

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tradingsys"]
