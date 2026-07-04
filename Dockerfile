FROM ghcr.io/jellyfin/jellyfin:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV FFMPEG_LOCATION=/usr/lib/jellyfin-ffmpeg

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        curl \
        socat \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Make Jellyfin's ffmpeg tools visible on PATH as a fallback.
RUN ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe

WORKDIR /app

COPY requirements.txt .
RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot/ ./bot/
COPY entrypoint.sh .
RUN chmod +x /app/entrypoint.sh

RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

RUN useradd --no-create-home --shell /bin/false botuser \
    && chown -R botuser:botuser /app

USER botuser

ENTRYPOINT ["/app/entrypoint.sh"]
