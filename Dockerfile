# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

# Set working directory
WORKDIR /app

# Copy package metadata and source so the local editable install can resolve
COPY pyproject.toml .
COPY requirements.txt .

# Install third-party dependencies in a cacheable layer. The source tree is
# copied below so normal code/template edits do not reinstall every package.
RUN --mount=type=cache,target=/root/.cache/pip \
    sed '/^-e \.$/d' requirements.txt > /tmp/requirements-runtime.txt \
    && pip install -r /tmp/requirements-runtime.txt

# Copy application code and install this project without re-resolving deps.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps -e .
COPY src/dihi/ ./
# Include the browser extension for the /extension.zip download endpoint.
COPY extension ./extension

# Create directories for data persistence
RUN mkdir -p /app/merged /app/data

# Default environment variables
ENV PORT=5000
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Run the application with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "app3:app"]
