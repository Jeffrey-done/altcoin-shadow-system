FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (excluding what's in .dockerignore)
COPY . .

# Data directory is expected to be mounted at runtime
RUN mkdir -p /app/backtest_cache /app/data

# Entrypoint: ensure runtime data files exist before starting services
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]

EXPOSE 8080
# Default: run dashboard. docker-compose overrides with `command:` per service
CMD ["python3", "dashboard.py"]
