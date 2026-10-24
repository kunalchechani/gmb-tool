# Production Dockerfile for Dokploy Deployment
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for psycopg2 and healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure upload directory exists with proper permissions
RUN mkdir -p /app/static/uploads

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PORT=5000 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:5000/healthz || exit 1

# Production WSGI server
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "3", "--threads", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
