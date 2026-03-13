# ──────────────────────────────────────────────────────────────
# Copilot Dispatch API — Production Docker Image
# Multi-stage build: builder installs dependencies, runner
# copies only installed packages onto a minimal base image.
# ──────────────────────────────────────────────────────────────

# Stage 1: Builder — install Python dependencies into a clean prefix
# AppRunner runs on x86_64 — explicit platform prevents architecture
# mismatches when building on Apple-Silicon (arm64) hosts.
FROM --platform=linux/amd64 python:3.13-slim AS builder

WORKDIR /build

# Install build-time system dependencies (gcc for potential native extensions).
# These are NOT present in the final runner image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first for Docker layer caching.
# Dependencies are only reinstalled when pyproject.toml changes.
COPY pyproject.toml ./

# Copy application package so the editable install resolves correctly
COPY app/ ./app/

# Install production dependencies into a dedicated prefix.
# --no-cache-dir prevents pip cache from bloating the layer.
RUN pip install --no-cache-dir --prefix=/install .

# ──────────────────────────────────────────────────────────────
# Stage 2: Runner — minimal runtime image
# ──────────────────────────────────────────────────────────────
FROM --platform=linux/amd64 python:3.13-slim AS runner

# Create a non-root user for security — the application process
# must not run as root in production.
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy installed Python packages from the builder stage
COPY --from=builder /install /usr/local

# Copy application source code
COPY app/ ./app/

# Set ownership so the non-root user can read all application files
RUN chown -R appuser:appuser /app

# Switch to non-root user for runtime
USER appuser

EXPOSE 8000

# Health check using Python stdlib urllib — avoids installing curl/wget
# in the slim image, keeping the image footprint minimal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.src.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
