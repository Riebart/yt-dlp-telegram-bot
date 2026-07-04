FROM ubuntu:latest

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies:
#   python3 + pip   -- runtime
#   ffmpeg          -- required by yt-dlp to mux video/audio streams
#   curl            -- used below to fetch the yt-dlp binary from GitHub
#   ca-certificates -- HTTPS support for curl and yt-dlp
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ffmpeg \
        curl \
        socat \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies into a venv -- isolated from the system Python.
COPY requirements.txt .
RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot/ ./bot/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Install yt-dlp from the official GitHub release binary so it tracks the
# latest stable version (the apt/pip packages often lag behind).
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

# Drop privileges -- never run a long-lived bot as root.
RUN useradd --no-create-home --shell /bin/false botuser \
    && chown -R botuser:botuser /app

USER botuser

# The bot uses long-polling -- no port needs to be exposed.
# Supply config via environment variables or a mounted .env file:
#
#   docker run -d --env-file .env --restart unless-stopped yt-dlp-bot
#
# To persist downloaded files between restarts (optional):
#   docker run -d --env-file .env -v /host/downloads:/tmp --restart unless-stopped yt-dlp-bot

ENTRYPOINT ["/app/entrypoint.sh"]
