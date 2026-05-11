FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir ccxt python-dotenv requests flask flask-socketio websocket-client

# Copy project files
COPY . .

# Create data directory
RUN mkdir -p /app/backtest_cache

# Default: run dashboard
EXPOSE 8080
CMD ["python3", "dashboard.py"]
