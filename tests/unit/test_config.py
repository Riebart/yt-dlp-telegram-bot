import pytest
import os
import sys
import bot2

def test_config_missing_token(monkeypatch):
    """Test that Config exits if TELEGRAM_BOT_TOKEN is missing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        bot2.Config()
    assert excinfo.value.code == 1

def test_config_defaults(monkeypatch):
    """Test Config default values."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    # Clear other env vars to ensure defaults are used
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("MAX_SIZE_MB", raising=False)
    
    cfg = bot2.Config()
    assert cfg.bot_token == "test_token"
    assert cfg.allowed_chat_ids == set()
    assert cfg.max_size_mb == 50
    assert cfg.compress_mb == 45
    assert cfg.audio_bps == 96000
    assert cfg.video_bitrate_kbps == 2000
    assert cfg.min_video_bitrate_kbps == 800
    assert cfg.preflight_duration_min == 7
    assert cfg.download_timeout == 300
    assert cfg.ffmpeg_location == ""
    assert cfg.save_dir == ""
    assert cfg.ollama_model == "qwen3.5:0.8b"
    assert cfg.ollama_tcp_host == "127.0.0.1"
    assert cfg.ollama_tcp_port == "11434"
    assert cfg.ollama_host == "http://127.0.0.1:11434"
    assert cfg.ollama_timeout == 30
    assert cfg.log_level == "INFO"

def test_config_custom_values(monkeypatch):
    """Test Config with custom environment variables."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "custom_token")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "1,2, 3 ")
    monkeypatch.setenv("MAX_SIZE_MB", "100")
    monkeypatch.setenv("COMPRESS_MB", "90")
    monkeypatch.setenv("AUDIO_BPS", "128000")
    monkeypatch.setenv("VIDEO_BITRATE_KBPS", "3000")
    monkeypatch.setenv("MIN_VIDEO_BITRATE_KBPS", "500")
    monkeypatch.setenv("PREFLIGHT_DURATION_MIN", "15")
    monkeypatch.setenv("DOWNLOAD_TIMEOUT", "600")
    monkeypatch.setenv("FFMPEG_LOCATION", "/usr/bin/ffmpeg")
    monkeypatch.setenv("SAVE_DIR", "./downloads")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3")
    monkeypatch.setenv("OLLAMA_LISTEN_TCP_HOST", "0.0.0.0")
    monkeypatch.setenv("OLLAMA_LISTEN_TCP_PORT", "11435")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "60")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    cfg = bot2.Config()
    assert cfg.bot_token == "custom_token"
    assert cfg.allowed_chat_ids == {1, 2, 3}
    assert cfg.max_size_mb == 100
    assert cfg.compress_mb == 90
    assert cfg.audio_bps == 128000
    assert cfg.video_bitrate_kbps == 3000
    assert cfg.min_video_bitrate_kbps == 500
    assert cfg.preflight_duration_min == 15
    assert cfg.download_timeout == 600
    assert cfg.ffmpeg_location == "/usr/bin/ffmpeg"
    assert cfg.save_dir == "./downloads"
    assert cfg.ollama_model == "llama3"
    assert cfg.ollama_tcp_host == "0.0.0.0"
    assert cfg.ollama_tcp_port == "11435"
    assert cfg.ollama_host == "http://0.0.0.0:11435"
    assert cfg.ollama_timeout == 60
    assert cfg.log_level == "DEBUG"

def test_config_log_startup(mock_config, caplog):
    """Test log_startup executes without error and logs key info."""
    with caplog.at_level("INFO"):
        mock_config.log_startup()
    assert "yt-dlp Telegram bot starting" in caplog.text
    assert "MAX_SIZE_MB" in caplog.text

def test_config_empty_allowed_ids(monkeypatch, caplog):
    """Test ALLOWED_CHAT_IDS with empty string or only whitespace."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "  ")
    cfg = bot2.Config()
    assert cfg.allowed_chat_ids == set()
    
    with caplog.at_level("WARNING"):
        cfg.log_startup()
    assert "ALLOWED_CHAT_IDS not set — ANY user can trigger downloads!" in caplog.text

def test_config_log_startup_warning(monkeypatch, caplog):
    """Test that log_startup warns when ALLOWED_CHAT_IDS is not set."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    cfg = bot2.Config()
    with caplog.at_level("WARNING"):
        cfg.log_startup()
    assert "ALLOWED_CHAT_IDS not set — ANY user can trigger downloads!" in caplog.text
