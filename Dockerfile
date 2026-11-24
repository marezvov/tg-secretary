FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies (ffmpeg for whisper audio, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    ca-certificates \
    libgomp1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install python requirements
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Install Playwright browser and its Linux OS dependencies
RUN playwright install chromium && \
    playwright install-deps chromium

# Copy application files
COPY app/ ./app/

# Create data directory
RUN mkdir -p /app/data

VOLUME ["/app/data"]

CMD ["python", "-m", "app.main"]
