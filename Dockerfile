# Stage 1: Builder
FROM python:3.11-alpine as builder

WORKDIR /app

# Install build dependencies (needed for some python packages)
RUN apk add --no-cache gcc musl-dev libffi-dev

COPY requirements.txt .
# Install dependencies to a specific location
RUN pip install --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-alpine

WORKDIR /app

# Create a non-root user
RUN adduser -D -u 1000 appuser

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY server.py easynews_client.py ./

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://localhost:8081/health || exit 1

# Environment Defaults
ENV PORT=8081
ENV PYTHONUNBUFFERED=1

# Run the server
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8081"]
