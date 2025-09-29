# -------- build stage --------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build tools and Cairo dependencies for xhtml2pdf
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc git curl \
    pkg-config libcairo2-dev libpango1.0-dev libpangocairo-1.0-0 \
  && rm -rf /var/lib/apt/lists/*

# Copy requirements from repo root (adjust path if yours differs)
COPY requirements.txt .

# Create and use a venv to keep runtime slim
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app

# -------- runtime stage --------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Create a non-root user
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy venv from builder (contains all installed packages)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy app source
COPY --from=builder /app /app

# Use non-root user
RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8080

# FastAPI entrypoint: adjust if your app variable or module path differs
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:8080"]