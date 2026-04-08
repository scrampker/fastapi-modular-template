# ---------------------------------------------------------------------------
# Multi-stage Docker build for a FastAPI application.
# Stage 1 (builder): installs Python dependencies into /install.
# Stage 2 (runtime): minimal image with only what's needed to run.
# ---------------------------------------------------------------------------

# ---- Build stage -----------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build-time system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specification and install packages into a prefix directory
# so they can be cleanly copied into the runtime image.
COPY pyproject.toml ./
COPY app/ app/
RUN pip install --no-cache-dir --prefix=/install .

# ---- Runtime stage ---------------------------------------------------------
FROM python:3.12-slim

ARG APP_PORT=8000
ENV APP_PORT=${APP_PORT}

WORKDIR /app

# Install runtime system dependencies
# curl is required for the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from the builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY app/ app/

# Create a non-root user and hand ownership of the working directory to it
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser && \
    chown -R appuser:appuser /app

USER appuser

# Python runtime settings
ENV PYTHONUNBUFFERED=1

EXPOSE ${APP_PORT}

# Liveness / readiness probe — the /health endpoint must return HTTP 2xx.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${APP_PORT}/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
