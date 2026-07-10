# Multi-arch image (build with: docker buildx build --platform linux/amd64,linux/arm64 ...)
# Python 3.12 is the project's target runtime.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ADVTOOLS_DATA_DIR=/data/crawls

# advertools/Scrapy pull lxml; wheels cover both arches, but keep a tiny toolchain
# fallback for source builds on uncommon platforms.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# Persistent artefact + job store. Mount a volume here in production.
RUN mkdir -p /data/crawls
VOLUME ["/data/crawls"]

# Non-root for safety.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /data/crawls
USER appuser

EXPOSE 8000

# Defaults to stdio; set ADVTOOLS_TRANSPORT=http (+ bearer token + allowlist) for remote.
ENTRYPOINT ["python", "-m", "advertools_mcp"]
