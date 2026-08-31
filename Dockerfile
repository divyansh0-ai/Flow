# ---------------------------------------------------------------------------
# Flow Agent — Cloud Run container
# ---------------------------------------------------------------------------
# Slim, single-stage build tuned for Cloud Run:
#   * non-root user
#   * layer-cached dependency install
#   * honors the platform-injected $PORT
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Python runtime hygiene
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# git is required at runtime: the agentic loop clones the repository under
# repair into a temporary workspace so it can run its test suite.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better build-cache reuse.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY agent.py main.py agentic.py workspace.py github_client.py ./
COPY static/ ./static/

# Run as a non-root user (Cloud Run best practice).
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Cloud Run sets $PORT; uvicorn binds to it. Use shell form so $PORT expands.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
