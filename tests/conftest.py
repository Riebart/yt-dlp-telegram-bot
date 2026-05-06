import pytest
import os
import sys
from unittest.mock import MagicMock

# Ensure the root directory is in sys.path so we can import bot2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bot2

@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset global variables in bot2 before each test."""
    bot2.URL_CACHE.clear()
    bot2.ACTIVE_PROCESSES.clear()
    bot2.CANCELLATIONS.clear()
    yield
    bot2.URL_CACHE.clear()
    bot2.ACTIVE_PROCESSES.clear()
    bot2.CANCELLATIONS.clear()

@pytest.fixture
def mock_config(monkeypatch):
    """Provides a Config instance with default test values."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "123,456")
    return bot2.Config()

@pytest.fixture
def mock_ffmpeg(mock_config):
    """Provides an FFmpegProcessor instance."""
    return bot2.FFmpegProcessor(mock_config)

@pytest.fixture
def mock_downloader(mock_config):
    """Provides a YtdlpDownloader instance."""
    return bot2.YtdlpDownloader(mock_config)

@pytest.fixture
def mock_classifier(mock_config):
    """Provides an OllamaClassifier instance."""
    return bot2.OllamaClassifier(mock_config)
