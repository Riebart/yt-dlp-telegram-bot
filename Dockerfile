FROM ubuntu:latest

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Force the iHD VAAPI driver (required for Gen8+ Intel GPUs with QSV).
# The older i965 driver does not support h264_qsv on modern hardware.
ENV LIBVA_DRIVER_NAME=iHD

# Install system dependencies:
#   python3 + pip                  -- runtime
#   ffmpeg                         -- required by yt-dlp to mux video/audio streams
#   curl                           -- used below to fetch the yt-dlp binary from GitHub
#   ca-certificates                -- HTTPS support for curl and yt-dlp
#   intel-media-va-driver-non-free -- iHD VAAPI driver, required for QSV on Gen8+ GPUs
#   libmfx1                        -- Intel Media SDK (MSDK) runtime; ffmpeg h264_qsv uses this
#   libmfx-tools                   -- includes mfxinfo for diagnosing MFX session issues
#   vainfo                         -- verify VA-API surfaces are visible inside the container
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ffmpeg \
        curl \
        socat \
        ca-certificates \
        intel-media-va-driver-non-free \
        libmfx1 \
        libmfx-tools \
        vainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot/ ./bot/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

RUN useradd --no-create-home --shell /bin/false botuser \
    && chown -R botuser:botuser /app

# Create the render group (GID 109 is the Ubuntu standard) and add botuser to
# it so it can open /dev/dri/renderD128 for QSV hardware acceleration.
# If your host render GID differs from 109, pass --group-add at docker run time.
RUN groupadd -g 109 render 2>/dev/null || true \
    && usermod -aG render botuser

USER botuser

# To enable Intel QSV hardware acceleration, pass the GPU device and ensure
# the render group GID matches your host:
#
#   docker run -d --env-file .env \
#     --device /dev/dri/renderD128 \
#     --group-add $(getent group render | cut -d: -f3) \
#     --restart unless-stopped yt-dlp-bot
#
# QSV is optional -- the bot falls back to libx264 if the device is absent
# or the MFX session cannot be opened.

ENTRYPOINT ["/app/entrypoint.sh"]
