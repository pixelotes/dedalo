# syntax=docker/dockerfile:1
# ------------------------------------------------------------------------------
# Dédalo - Ultralight visual workflow orchestrator
# Multi-stage build optimized for low memory usage and maximum security
# ------------------------------------------------------------------------------

# ==============================================================================
# Stage 1: Builder (Dependency installation in an isolated venv)
# ==============================================================================
FROM python:3.15-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY dedalo/ ./dedalo/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ==============================================================================
# Stage 2: Runner (Minimal, secure final image without build tools)
# ==============================================================================
FROM python:3.15-slim-bookworm AS runner

# Command isolation (DEDALO_SANDBOX): bubblewrap is installed by default (~100 KB).
# For an image without it: docker build --build-arg WITH_BWRAP=0 .
ARG WITH_BWRAP=1

# Install required system binaries (git for version control nodes)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        openssh-client \
        $([ "$WITH_BWRAP" = "1" ] && echo bubblewrap) \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

# User arguments (UID/GID 1000 by default, standard on Raspberry Pi / Ubuntu)
ARG UID=1000
ARG GID=1000

# Security best practice: unprivileged system user
RUN groupadd -g "${GID}" -r dedalo && \
    useradd -u "${UID}" -r -g dedalo -d /app -s /usr/sbin/nologin -c "Dedalo Service User" dedalo

WORKDIR /app

# Copy the optimized virtual environment from the builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Performance and memory environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=1 \
    MALLOC_TRIM_THRESHOLD_=65536 \
    DEDALO_HOST=0.0.0.0 \
    DEDALO_PORT=8080

# Copy source code, assigning ownership to the unprivileged user
COPY --chown=dedalo:dedalo dedalo /app/dedalo

# Prepare the data directory with strict permissions
RUN mkdir -p /app/data && chown -R dedalo:dedalo /app

# Switch to the unprivileged user
USER dedalo

# Expose the service port
EXPOSE 8080

# Declare the data volume
VOLUME ["/app/data"]

# Native Python health check (no extra dependencies or curl vulnerabilities)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1

# Clean handling of shutdown signals
STOPSIGNAL SIGTERM

# Default startup command (optimized web server)
CMD ["uvicorn", "dedalo.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--limit-concurrency", "15", "--timeout-keep-alive", "5"]
