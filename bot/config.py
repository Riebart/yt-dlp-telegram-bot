import os
import logging

log = logging.getLogger("bot.config")

class Config:
    """All configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.allowed_chat_ids: set[int] = set()
        allowed = os.environ.get("ALLOWED_CHAT_IDS")
        if allowed and allowed.strip():
            try:
                self.allowed_chat_ids = {int(x.strip()) for x in allowed.split(",")}
            except ValueError:
                log.error("Invalid ALLOWED_CHAT_IDS format. Expected comma-separated integers.")

        self.download_timeout: int = int(os.environ.get("DOWNLOAD_TIMEOUT", "3600"))
        self.max_size_mb: float = float(os.environ.get("MAX_SIZE_MB", "50"))
        self.compress_mb: float = float(os.environ.get("COMPRESS_MB", "40"))
        self.min_video_bitrate_kbps: int = int(os.environ.get("MIN_VIDEO_BITRATE_KBPS", "250"))
        self.audio_bps: int = int(os.environ.get("AUDIO_BPS", "128000"))

        self.preflight_duration_min: int = int(os.environ.get("PREFLIGHT_DURATION_MIN", "30"))
        self.ffmpeg_location: str | None = os.environ.get("FFMPEG_LOCATION")
        self.save_dir: str | None = os.environ.get("SAVE_DIR")

        # Ollama Config
        self.ollama_model: str = os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b")
        self.ollama_tcp_host: str = os.environ.get("OLLAMA_LISTEN_TCP_HOST", "127.0.0.1")
        self.ollama_tcp_port: str = os.environ.get("OLLAMA_LISTEN_TCP_PORT", "11434")
        self.ollama_host: str = os.environ.get("OLLAMA_HOST", f"http://{self.ollama_tcp_host}:{self.ollama_tcp_port}")
        self.ollama_timeout: int = int(os.environ.get("OLLAMA_TIMEOUT", "30"))
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        """Validate critical configuration."""
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required.")

    def log_startup(self) -> None:
        """Log the current configuration at startup."""
        log.info("yt-dlp Telegram bot starting")
        log.info("MAX_SIZE_MB=%s, COMPRESS_MB=%s", self.max_size_mb, self.compress_mb)
        log.info("OLLAMA_MODEL=%s, OLLAMA_HOST=%s", self.ollama_model, self.ollama_host)
        if not self.allowed_chat_ids:
            log.warning("ALLOWED_CHAT_IDS not set — ANY user can trigger downloads!")
