import pytest
import os
import sys
from unittest.mock import MagicMock

# Ensure the root directory is in sys.path so we can import bot
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import Config
from bot.downloader import YtdlpDownloader
from bot.processor import FFmpegProcessor
from bot.classifier import OllamaClassifier
from bot.state import URL_CACHE, ACTIVE_PROCESSES, CANCELLATIONS

@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset global variables in bot before each test."""
    URL_CACHE.clear()
    ACTIVE_PROCESSES.clear()
    CANCELLATIONS.clear()
    yield
    URL_CACHE.clear()
    ACTIVE_PROCESSES.clear()
    CANCELLATIONS.clear()

@pytest.fixture
def mock_config(monkeypatch):
    """Provides a Config instance with default test values."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "123,456")
    return Config()

@pytest.fixture
def mock_ffmpeg(mock_config):
    """Provides an FFmpegProcessor instance."""
    return FFmpegProcessor(mock_config)

@pytest.fixture
def mock_downloader(mock_config):
    """Provides a YtdlpDownloader instance."""
    return YtdlpDownloader(mock_config)

@pytest.fixture
def mock_classifier(mock_config):
    """Provides an OllamaClassifier instance."""
    return OllamaClassifier(mock_config)
