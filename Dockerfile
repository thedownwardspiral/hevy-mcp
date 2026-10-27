# syntax=docker/dockerfile:1

# Build stage: resolve the locked dependencies and install the server into a
# virtualenv with uv. Nothing from this stage but the virtualenv is kept.
FROM python:3.14-slim@sha256:c2aca8207dfb68e7d20fecb7fad7e145c5d79d2ff36712d416c144130606d71d AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Runtime stage: the same slim Python, the virtualenv and a non-root user.
FROM python:3.14-slim@sha256:c2aca8207dfb68e7d20fecb7fad7e145c5d79d2ff36712d416c144130606d71d
LABEL org.opencontainers.image.title="hevy-mcp" \
      org.opencontainers.image.description="Read-only MCP server for Hevy workout and body-measurement data" \
      org.opencontainers.image.source="https://github.com/thedownwardspiral/hevy-mcp"
RUN groupadd --system --gid 10001 hevy \
 && useradd --system --uid 10001 --gid hevy --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin hevy
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('MCP_PORT', '8000')}/health\", timeout=4)"]
ENTRYPOINT ["hevy-mcp"]
