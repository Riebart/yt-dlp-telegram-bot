import pytest
from unittest.mock import MagicMock, AsyncMock, mock_open
from pathlib import Path
import tempfile
import asyncio
import shutil
import os
import time
import io
import signal

from bot.config import Config
from bot.downloader import YtdlpDownloader
from bot.processor import FFmpegProcessor, LargeVideoSplitter
from bot.router import BotRouter
from bot.handlers.download import DownloadIntentHandler
from bot.handlers.audio import AudioIntentHandler
from bot.handlers.report import ReportSizeIntentHandler
from bot.state import CANCELLATIONS, URL_CACHE, ACTIVE_PROCESSES
from bot.utils import markdown_escape, track_process, untrack_process

@pytest.fixture(autouse=True)
def reset_state():
    CANCELLATIONS.clear()
    URL_CACHE.clear()
    ACTIVE_PROCESSES.clear()
    yield
    CANCELLATIONS.clear()
    URL_CACHE.clear()
    ACTIVE_PROCESSES.clear()

@pytest.fixture
def mock_update():
    update = MagicMock()
    update.effective_chat.id = 123
    update.effective_user.id = 123
    update.message.chat_id = 123
    update.message.message_id = 456
    update.message.text = "https://example.com"
    update.message.from_user.username = "testuser"
    update.message.from_user.is_bot = False
    update.message.reply_text = AsyncMock()
    update.message.reply_video = AsyncMock()
    update.message.reply_audio = AsyncMock()
    return update

@pytest.fixture
def mock_context():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot.send_video = AsyncMock()
    context.bot.send_audio = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.send_chat_action = AsyncMock()
    return context

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_download_handler_url_missing(mock_config, mock_update, mock_context):
    handler = DownloadIntentHandler(mock_config, MagicMock(), MagicMock())
    await handler.handle(mock_update.message, mock_context, "no url here")
    mock_update.message.reply_text.assert_called_with("⚠️ I couldn't find a URL in your message.")

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_download_handler_info_fail(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(False, "metadata error", {}))
    handler = DownloadIntentHandler(mock_config, mock_dl, MagicMock())
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_update.message.reply_text.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_download_handler_pivot_to_report(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 1000, "filesize": 100*1024*1024}))
    mock_report = MagicMock()
    mock_report.send_report = AsyncMock()
    
    handler = DownloadIntentHandler(mock_config, mock_dl, MagicMock(), report_handler=mock_report)
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_report.send_report.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_download_handler_full_success(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {"title": "Vid", "ext": "mp4"}))
    
    mock_ff = MagicMock()
    handler = DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)
    
    video_file = tmp_path / "Vid.mp4"
    video_file.write_bytes(b"dummy_content")
    
    mocker.patch("builtins.open", mock_open(read_data=b"dummy"))
    mocker.patch("shutil.move")
    mocker.patch("shutil.copy")

    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_update.message.reply_video.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_download_handler_over_limit_compress(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {"title": "Vid", "ext": "mp4"}))
    
    mock_config.max_size_mb = 1
    video_file = tmp_path / "Vid.mp4"
    with open(video_file, "wb") as f:
        f.seek(5 * 1024 * 1024)
        f.write(b"\0")
    
    compressed_file = tmp_path / "compressed.mp4"
    compressed_file.write_bytes(b"dummy_content_small")

    mock_ff = MagicMock()
    mock_ff.get_duration = MagicMock(return_value=60)
    mock_ff.compress_to_size = MagicMock(return_value=(True, compressed_file, ""))
    
    handler = DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)
    
    mocker.patch("builtins.open", mock_open(read_data=b"dummy"))
    mocker.patch("shutil.move")
    mocker.patch("shutil.copy")
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_ff.compress_to_size.assert_called()
    mock_update.message.reply_video.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_audio_handler_full_path(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "title": "Song"}))
    mock_dl.download_audio_sync = MagicMock(return_value=(True, "", {"title": "Song"}))
    
    handler = AudioIntentHandler(mock_config, mock_dl)
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)
    
    song_file = tmp_path / "Song.mp3"
    song_file.write_bytes(b"dummy_audio_content")
    
    mocker.patch("builtins.open", mock_open())
    mocker.patch("shutil.copy")
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_update.message.reply_audio.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_bot_router_routing(mock_config, mock_update, mock_context, mocker):
    mock_classifier = MagicMock()
    mock_classifier.classify = AsyncMock(return_value=("download", "reply"))
    
    handler = MagicMock()
    handler.handle = AsyncMock()
    
    router = BotRouter(mock_config, mock_classifier, {"download": handler})
    
    mock_config.allowed_chat_ids = {123}
    mocker.patch("asyncio.sleep", AsyncMock())
    
    await router.handle_message(mock_update, mock_context)
    
    handler.handle.assert_called_with(mock_update.message, mock_context, "https://example.com")

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_bot_router_unauthorized(mock_config, mock_update, mock_context, mocker):
    mock_config.allowed_chat_ids = {456} 
    router = BotRouter(mock_config, MagicMock(), {})
    
    await router.handle_message(mock_update, mock_context)
    mock_update.message.reply_text.assert_called_with("⛔ You are not authorised to use this bot.")

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_download_handler_over_limit_split(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {"title": "Vid", "ext": "mp4"}))

    mock_config.max_size_mb = 1
    video_file = tmp_path / "Vid.mp4"
    with open(video_file, "wb") as f:
        f.seek(5 * 1024 * 1024)
        f.write(b"\0")

    mock_ff = MagicMock()
    mock_ff.get_duration = MagicMock(return_value=60)
    mock_ff.compress_to_size = MagicMock(return_value=(True, video_file, ""))
    mock_config.min_video_bitrate_kbps = 999999

    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)

    mock_part = tmp_path / "Vid.part1.mp4"
    mock_part.write_bytes(b"dummy_part")

    mock_splitter = MagicMock()
    mock_splitter.split_video = MagicMock(return_value=([mock_part], ""))
    mocker.patch("bot.processor.LargeVideoSplitter", return_value=mock_splitter)

    mocker.patch("builtins.open", mock_open(read_data=b"dummy"))
    mocker.patch("shutil.move")
    mocker.patch("shutil.copy")

    handler = DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    handler._splitter = mock_splitter

    mock_context.bot.send_video = AsyncMock()
    status_msg = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg

    try:
        await handler.handle(mock_update.message, mock_context, "https://example.com")
    except Exception:
        pass

    mock_splitter.split_video.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_bot_router_callback_exhaustive(mock_config, mock_context, mocker):
    actions = ["dl", "au", "cp", "sp", "cn"]
    for action in actions:
        update = MagicMock()
        update.callback_query.data = f"{action}:test_uid"
        update.callback_query.message.chat_id = 123
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.from_user.is_bot = True 
        
        URL_CACHE["test_uid"] = {"url": "https://example.com"}
        
        h_dl = MagicMock(); h_dl.handle = AsyncMock()
        h_au = MagicMock(); h_au.handle = AsyncMock()
        
        router = BotRouter(mock_config, MagicMock(), {
            "download": h_dl,
            "audio": h_au,
        })
        
        await router.handle_callback(update, mock_context)
        
        if action == "dl": h_dl.handle.assert_called()
        elif action == "au": h_au.handle.assert_called()
        elif action == "cn": assert "test_uid" not in URL_CACHE

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_bot_router_handle_message_no_text(mock_config, mock_update, mock_context, mocker):
    mock_update.message.text = None
    mock_update.message.caption = None
    router = BotRouter(mock_config, MagicMock(), {})
    await router.handle_message(mock_update, mock_context)

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_bot_router_no_handler(mock_config, mock_update, mock_context, mocker):
    mock_classifier = MagicMock()
    mock_classifier.classify = AsyncMock(return_value=("unknown_intent", "Decline message"))
    router = BotRouter(mock_config, mock_classifier, {})
    mock_config.allowed_chat_ids = {123}
    mocker.patch("asyncio.sleep", AsyncMock())
    await router.handle_message(mock_update, mock_context)
    mock_update.message.reply_text.assert_any_call("Decline message")

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_bot_router_handle_cancel_empty(mock_config, mock_update, mock_context):
    router = BotRouter(mock_config, MagicMock(), {})
    ACTIVE_PROCESSES[123] = set()
    await router.handle_cancel(mock_update, mock_context)
    mock_update.message.reply_text.assert_called_with("🛑 Cancelled 0 active task(s).")

def test_track_untrack_process():
    chat_id = 456
    proc = MagicMock()
    track_process(chat_id, proc, ACTIVE_PROCESSES)
    assert proc in ACTIVE_PROCESSES[chat_id]
    untrack_process(chat_id, proc, ACTIVE_PROCESSES)
    assert chat_id not in ACTIVE_PROCESSES

@pytest.mark.asyncio
async def test_cleanup_cache_task(mocker):
    URL_CACHE["old"] = {"time": time.monotonic() - 4000, "url": "..."}
    URL_CACHE["new"] = {"time": time.monotonic(), "url": "..."}
    
    async def dummy_sleep(seconds):
        pass

    stop_called = False
    async def sleep_side_effect(seconds):
        nonlocal stop_called
        if stop_called:
            raise asyncio.CancelledError()
        stop_called = True
        await dummy_sleep(0)

    mocker.patch("asyncio.sleep", side_effect=sleep_side_effect)
    
    try:
        from bot.state import cleanup_cache_task
        await cleanup_cache_task()
    except (asyncio.CancelledError, Exception):
        pass
    
    assert "old" not in URL_CACHE
    assert "new" in URL_CACHE

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_report_handler_no_url(mock_config, mock_update, mock_context):
    handler = ReportSizeIntentHandler(mock_config, MagicMock())
    await handler.handle(mock_update.message, mock_context, "just some text")
    mock_update.message.reply_text.assert_called_with("⚠️ No URL found.")

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_report_handler_info_fail(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(False, "metadata error", {}))
    handler = ReportSizeIntentHandler(mock_config, mock_dl)
    
    status_msg = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    status_msg.edit_text.assert_called_with("❌ Could not fetch info: metadata error")

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_report_handler_success_no_rec(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {
        "title": "Test Video", 
        "duration": 60, 
        "filesize": 10 * 1024 * 1024 # 10MB, well within limit
    }))
    handler = ReportSizeIntentHandler(mock_config, mock_dl)
    
    status_msg = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    
    # Verify report content (ignoring specific formatting but checking key elements)
    call_args = status_msg.edit_text.call_args[0][0]
    assert "Test Video" in call_args
    assert "Duration**: 1m 0s" in call_args
    assert "Size**: 10.0 MiB" in call_args
    assert "Recommendation" not in call_args

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_report_handler_rec_compress(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    
    # Configure config for recommendation
    mock_config.compress_mb = 50
    mock_config.audio_bps = 128000
    mock_config.max_size_mb = 50
    mock_config.min_video_bitrate_kbps = 100
    
    mock_dl = MagicMock()
    # 100MB file, 60s duration
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {
        "title": "Big Video", 
        "duration": 60, 
        "filesize": 100 * 1024 * 1024 
    }))
    handler = ReportSizeIntentHandler(mock_config, mock_dl)
    
    status_msg = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    
    call_args = status_msg.edit_text.call_args[0][0]
    assert "Recommendation*: Compress to" in call_args
    assert "fits in 1 file" in call_args

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_report_handler_rec_split(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    
    # Set very high min_video_bitrate so it hits the floor
    mock_config.compress_mb = 1
    mock_config.audio_bps = 128000
    mock_config.max_size_mb = 1
    mock_config.min_video_bitrate_kbps = 10000 
    
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {
        "title": "Huge Video", 
        "duration": 60, 
        "filesize": 100 * 1024 * 1024 
    }))
    handler = ReportSizeIntentHandler(mock_config, mock_dl)
    
    status_msg = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    
    call_args = status_msg.edit_text.call_args[0][0]
    assert "Quality floor reached" in call_args
    assert "Split" in call_args


@pytest.mark.asyncio
async def test_downloader_progress_hook_cancel(mocker):
    dl = YtdlpDownloader(MagicMock())
    hook = dl._progress_hook(MagicMock(), {"chat_id": 123})
    CANCELLATIONS.add(123)
    with pytest.raises(Exception, match="Download cancelled"):
        hook({})
    CANCELLATIONS.remove(123)

@pytest.mark.asyncio
async def test_downloader_progress_hook_status(mocker):
    bot = AsyncMock()
    loop = MagicMock()
    dl = YtdlpDownloader(MagicMock())
    hook = dl._progress_hook(loop, {"chat_id": 123, "message_id": 456, "bot": bot})
    
    hook({"status": "finished", "total_bytes": 1024, "filename": "test.mp4"})
    hook({"status": "error", "error": "test error"})

@pytest.mark.asyncio
async def test_downloader_download_sync_exception(mocker):
    dl = YtdlpDownloader(MagicMock())
    mocker.patch("yt_dlp.YoutubeDL", side_effect=Exception("boom"))
    success, err, info = dl.download_sync("url", "tpl")
    assert not success
    assert "boom" in err

@pytest.mark.asyncio
async def test_downloader_download_audio_sync_exception(mocker):
    dl = YtdlpDownloader(MagicMock())
    mocker.patch("yt_dlp.YoutubeDL", side_effect=Exception("boom"))
    success, err, info = dl.download_audio_sync("url", "tpl")
    assert not success
    assert "boom" in err

@pytest.mark.asyncio
async def test_bot_router_handle_cancel_windows(mock_config, mock_update, mock_context, mocker):
    router = BotRouter(mock_config, MagicMock(), {})
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    ACTIVE_PROCESSES[123] = {mock_proc}
    
    mocker.patch("sys.platform", "win32")
    mock_run = mocker.patch("subprocess.run")
    
    await router.handle_cancel(mock_update, mock_context)
    
    mock_run.assert_called_with(["taskkill", "/F", "/T", "/PID", "9999"], capture_output=True)
    mock_update.message.reply_text.assert_called_with("🛑 Cancelled 1 active task(s).")

@pytest.mark.asyncio
async def test_bot_router_handle_cancel_linux(mock_config, mock_update, mock_context, mocker):
    router = BotRouter(mock_config, MagicMock(), {})
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    ACTIVE_PROCESSES[123] = {mock_proc}
    
    mocker.patch("sys.platform", "linux")
    mock_killpg = mocker.patch("os.killpg", create=True)
    mock_getpgid = mocker.patch("os.getpgid", create=True, return_value=8888)
    
    await router.handle_cancel(mock_update, mock_context)
    
    mock_killpg.assert_called_with(8888, signal.SIGTERM)
    mock_update.message.reply_text.assert_called_with("🛑 Cancelled 1 active task(s).")

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_bot_router_callback_expired(mock_config, mock_context):
    update = MagicMock()
    update.callback_query.data = "dl:expired_id"
    update.callback_query.message.chat_id = 123
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    router = BotRouter(mock_config, MagicMock(), {})
    await router.handle_callback(update, mock_context)
    update.callback_query.edit_message_text.assert_called_with("⚠️ This session has expired or the URL is no longer in cache.")

@pytest.mark.asyncio
async def test_error_handler(mocker):
    context = MagicMock()
    context.error = Exception("test error")
    mock_log = mocker.patch("bot.utils.log.error")
    from bot.utils import error_handler
    await error_handler(MagicMock(), context)
    mock_log.assert_called()

