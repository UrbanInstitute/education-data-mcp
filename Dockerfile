# Standalone image for the streamable-HTTP mode below. Build and run directly:
#   docker build -t edp-mcp .
#   docker run -p 8080:8080 edp-mcp

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies resolve from the lockfile alone, so this layer stays cached until
# the lockfile itself changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev --no-editable

COPY src/ ./src/
# --no-editable copies the package INTO the venv. Without it uv installs a .pth
# pointing at /app/src, and the runtime stage below — which copies only the venv
# — starts with ModuleNotFoundError.
RUN uv sync --locked --no-dev --no-editable


FROM python:3.14-slim-bookworm AS runtime

# Non-root: this server only ever reads, so it has no reason to run as root.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=build --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

USER app

ENV MCP_TRANSPORT=streamable-http \
    PORT=8080
EXPOSE 8080

# /health is a plain GET and needs no MCP handshake, unlike /mcp which speaks
# JSON-RPC over POST and answers a bare GET with an error.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\").read()"

# exec form so the server is PID 1 and receives SIGTERM directly — without it,
# stopping the container waits out the full kill timeout instead of shutting
# down cleanly.
ENTRYPOINT ["edp-mcp"]
