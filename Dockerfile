FROM node:22-bookworm-slim AS frontend

WORKDIR /build/frontend
RUN corepack enable && corepack prepare pnpm@10.33.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security
RUN sed -i "s#http://deb.debian.org/debian-security#${DEBIAN_SECURITY_MIRROR}#g; s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 install -y --no-install-recommends bash git openssh-client bubblewrap ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /bin/bash traceforge \
    && mkdir -p /data /config /projects /app \
    && chown traceforge:traceforge /data /config /projects

# Make Node.js/pnpm available to code tools as well as the frontend build.
COPY --from=frontend /usr/local/bin/ /usr/local/bin/
COPY --from=frontend /usr/local/lib/node_modules/ /usr/local/lib/node_modules/
WORKDIR /app
COPY pyproject.toml ./
COPY backend/traceforge/ ./backend/traceforge/
COPY infra/runner/tool_worker.py ./infra/runner/tool_worker.py
COPY infra/deploy/app.py ./infra/deploy/app.py
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -e .
# Corepack's build-stage download cache is not included in the runtime image.
RUN corepack disable && npm install --global pnpm@10.33.0

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRACEFORGE_DATA_DIR=/data \
    TRACEFORGE_CONFIG_DIR=/config
USER traceforge
EXPOSE 8001
# Caddy shares this network namespace and forwards only over loopback.
CMD ["python", "-m", "uvicorn", "infra.deploy.app:app", "--host", "127.0.0.1", "--port", "8001", "--workers", "1", "--no-proxy-headers"]
