import pytest
import asyncio
import os
import sys
import subprocess
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from bot.config import Config
from bot.downloader import YtdlpDownloader
from bot.processor import FFmpegProcessor
from bot.router import BotRouter
from bot.handlers.download import DownloadIntentHandler
from bot.handlers.audio import AudioIntentHandler
from bot.handlers.report import ReportSizeIntentHandler
from bot.state import URL_CACHE, CANCELLATIONS, ACTIVE_PROCESSES

# The original remote URL for the asset
REMOTE_TEST_URL = "https://www.w3schools.com/html/mov_bbb.mp4"

@pytest.fixture(scope="session")
def local_server(tmp_path_factory):
    """
    Downloads the test asset once and serves it locally via http.server.
    Returns the local URL of the asset.
    """
    # 1. Prepare local directory
    asset_dir = tmp_path_factory.mktemp("assets")
    local_file = asset_dir / "test_video.mp4"
    
    # 2. Download the file if not already there
    if not local_file.exists():
        import urllib.request
        with urllib.request.urlopen(REMOTE_TEST_URL) as response, open(local_file, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
    
    # 3. Start the server in the background
    port = 8000
    proc = subprocess.Popen(
        ["python", "-m", "http.server", str(port)],
        cwd=str(asset_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=True
    )
    
    # Give the server a moment to start
    time.sleep(1)
    
    local_url = f"http://localhost:{port}/test_video.mp4"
    
    yield local_url
    
    # 4. Cleanup: Terminate the server
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        # On Unix, kill the process group
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()

@pytest.fixture
def e2e_config():
    """Provides a mutable config for E2E tests."""
    cfg = Config()
    cfg.max_size_mb = 50
    cfg.compress_mb = 40
    cfg.audio_bps = 128000
    cfg.min_video_bitrate_kbps = 100
    return cfg

@pytest.fixture
def e2e_downloader(e2e_config):
    """Real downloader instance."""
    return YtdlpDownloader(e2e_config)

@pytest.fixture
def e2e_ffmpeg(e2e_config):
    """Real FFmpeg processor instance."""
    return FFmpegProcessor(e2e_config)

@pytest.fixture
def e2e_router(e2e_config, e2e_downloader, e2e_ffmpeg):
    """BotRouter initialized with real handlers for E2E testing."""
    report_h = ReportSizeIntentHandler(e2e_config, e2e_downloader)
    audio_h = AudioIntentHandler(e2e_config, e2e_downloader)
    download_h = DownloadIntentHandler(e2e_config, e2e_downloader, e2e_ffmpeg, report_handler=report_h)
    
    handlers = {
        "report": report_h,
        "audio": audio_h,
        "download": download_h
    }
    
    mock_classifier = MagicMock()
    async def simple_classify(text, duration=None, **kwargs):
        if "report" in text.lower(): return ("report", "Report requested")
        if "audio" in text.lower(): return ("audio", "Audio requested")
        return ("download", "Download requested")
    
    mock_classifier.classify = AsyncMock(side_effect=simple_classify)
    
    return BotRouter(e2e_config, mock_classifier, handlers)

@pytest.fixture
def temp_workspace(tmp_path):
    """A dedicated temporary directory for all file operations in a test."""
    return tmp_path

@pytest.fixture(autouse=True)
def reset_bot_state():
    """Clear all global state between E2E tests."""
    URL_CACHE.clear()
    CANCELLATIONS.clear()
    ACTIVE_PROCESSES.clear()
    yield
    URL_CACHE.clear()
    CANCELLATIONS.clear()
    ACTIVE_PROCESSES.clear()

@pytest.fixture(autouse=True)
def cleanup_zombies():
    """Ensure no zombie ffmpeg processes survive between tests."""
    yield
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe", "/T"], capture_output=True)
        subprocess.run(["taskkill", "/F", "/IM", "ffprobe.exe", "/T"], capture_output=True)
    else:
        # On Linux/Mac, find PIDs of ffmpeg and ffprobe and kill them
        try:
            pids = subprocess.check_output(["pgrep", "-f", "ffmpeg|ffprobe"]).decode().splitlines()
            for pid in pids:
                os.kill(int(pid), signal.SIGKILL)
        except (subprocess.CalledProcessError, Exception):
            pass

def log_async_call(mock_obj, call_name):
    """Wrapper to print calls to AsyncMocks for E2E visibility."""
    original_side_effect = mock_obj.side_effect
    
    async def wrapped(*args, **kwargs):
        # Log the call with the first argument (usually the text/path)
        arg_val = args[0] if args else kwargs.get("text", kwargs.get("video", kwargs.get("audio", "Unknown")))
        print(f"[BOT EVENT] {call_name}: {arg_val}")
        
        # Special handling for reply_text to ensure it returns a mock message with edit_text
        if call_name == "reply_text":
            return create_mock_message()
            
        if original_side_effect:
            return await original_side_effect(*args, **kwargs)
        return AsyncMock()
    
    mock_obj.side_effect = wrapped
    return mock_obj

def create_mock_message():
    """Creates a mock message object with logged edit_text and delete methods."""
    msg = MagicMock()
    # edit_text must be an AsyncMock because it is awaited
    msg.edit_text = AsyncMock()
    log_async_call(msg.edit_text, "edit_text")
    
    # delete must be an AsyncMock because it is awaited
    msg.delete = AsyncMock()
    log_async_call(msg.delete, "delete")
    
    return msg

@pytest.fixture
def mock_update():
    """Mocked Telegram Update object with event logging."""
    update = MagicMock()
    update.effective_chat.id = 123
    update.effective_user.id = 123
    update.message.chat_id = 123
    update.message.message_id = 456
    update.message.text = "https://example.com"
    
    # Setup reply methods with logging
    update.message.reply_text = AsyncMock(return_value=create_mock_message())
    log_async_call(update.message.reply_text, "reply_text")
    
    update.message.reply_video = AsyncMock()
    log_async_call(update.message.reply_video, "reply_video")
    
    update.message.reply_audio = AsyncMock()
    log_async_call(update.message.reply_audio, "reply_audio")
    
    return update

@pytest.fixture
def mock_context():
    """Mocked Telegram Context object with event logging."""
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    log_async_call(context.bot.send_message, "bot.send_message")
    
    context.bot.edit_message_text = AsyncMock()
    log_async_call(context.bot.edit_message_text, "bot.edit_message_text")
    
    context.bot.send_video = AsyncMock()
    log_async_call(context.bot.send_video, "bot.send_video")
    
    context.bot.send_audio = AsyncMock()
    log_async_call(context.bot.send_audio, "bot.send_audio")
    
    context.bot.send_chat_action = AsyncMock()
    log_async_call(context.bot.send_chat_action, "bot.send_chat_action")
    
    return context
