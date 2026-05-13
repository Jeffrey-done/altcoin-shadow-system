FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (excluding what's in .dockerignore)
COPY . .

# Data directory is expected to be mounted at runtime
RUN mkdir -p /app/backtest_cache /app/data

EXPOSE 8080
# Default: run dashboard. docker-compose overrides with `command:` per service
CMD ["python3", "dashboard.py"]
