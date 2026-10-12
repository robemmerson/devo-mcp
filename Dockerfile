# Devo MCP server: streamable HTTP on port 8000, bearer-key auth (MCP_API_KEY).
FROM ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 AS uv

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin devo \
 && mkdir -p /data && chown devo /data && chmod 700 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    DEVO_CACHE_DIR=/data/cache \
    DEVO_MCP_WORKDIR=/data/work \
    DEVO_MCP_TRANSPORT=http \
    DEVO_MCP_HOST=0.0.0.0 \
    DEVO_MCP_PORT=8000
# Credentials (DEVO_TOKEN, DEVO_REGION, MCP_API_KEY) come from the runtime environment, never the image.
USER devo
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["devo-mcp"]
