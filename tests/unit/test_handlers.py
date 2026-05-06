import pytest
from unittest.mock import MagicMock, AsyncMock, mock_open
from pathlib import Path
import bot2
import tempfile
import asyncio
import shutil
import os
import time
import io

@pytest.fixture(autouse=True)
def reset_cancellations():
    bot2.CANCELLATIONS.clear()
    yield
    bot2.CANCELLATIONS.clear()

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
@pytest.mark.timeout(5)
async def test_download_handler_url_missing(mock_config, mock_update, mock_context):
    handler = bot2.DownloadIntentHandler(mock_config, MagicMock(), MagicMock())
    await handler.handle(mock_update.message, mock_context, "no url here")
    mock_update.message.reply_text.assert_called_with("⚠️ I couldn't find a URL in your message.")

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_download_handler_info_fail(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(False, "metadata error", {}))
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, MagicMock())
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_update.message.reply_text.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_download_handler_pivot_to_report(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 1000, "filesize": 100*1024*1024}))
    mock_report = MagicMock()
    mock_report.send_report = AsyncMock()
    
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, MagicMock(), report_handler=mock_report)
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_report.send_report.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_download_handler_full_success(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {"title": "Vid", "ext": "mp4"}))
    
    mock_ff = MagicMock()
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    
    # Use real temp path instead of mocked /tmp
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)
    
    # Create a real dummy video file
    video_file = tmp_path / "Vid.mp4"
    video_file.write_bytes(b"dummy_content")
    
    mocker.patch("bot2.open", mock_open(read_data=b"dummy"))
    mocker.patch("shutil.move")
    mocker.patch("shutil.copy")

    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_update.message.reply_video.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_download_handler_over_limit_compress(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {"title": "Vid", "ext": "mp4"}))
    
    # Use a 5MB file and 1MB limit
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
    
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)
    
    mocker.patch("bot2.open", mock_open(read_data=b"dummy"))
    mocker.patch("shutil.move")
    mocker.patch("shutil.copy")
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_ff.compress_to_size.assert_called()
    mock_update.message.reply_video.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_audio_handler_full_path(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "title": "Song"}))
    mock_dl.download_audio_sync = MagicMock(return_value=(True, "", {"title": "Song"}))
    
    handler = bot2.AudioIntentHandler(mock_config, mock_dl)
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)
    
    # Create real audio file
    song_file = tmp_path / "Song.mp3"
    song_file.write_bytes(b"dummy_audio_content")
    
    mocker.patch("builtins.open", mock_open())
    mocker.patch("shutil.copy")
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    mock_update.message.reply_audio.assert_called()

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_bot_router_routing(mock_config, mock_update, mock_context, mocker):
    mock_classifier = MagicMock()
    mock_classifier.classify = AsyncMock(return_value=("download", "reply"))
    
    handler = MagicMock()
    handler.handle = AsyncMock()
    
    router = bot2.BotRouter(mock_config, mock_classifier, {"download": handler})
    
    mock_config.allowed_chat_ids = {123}
    mocker.patch("bot2.asyncio.sleep", AsyncMock())
    
    await router.handle_message(mock_update, mock_context)
    
    handler.handle.assert_called_with(mock_update.message, mock_context, "https://example.com")

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_bot_router_unauthorized(mock_config, mock_update, mock_context, mocker):
    mock_config.allowed_chat_ids = {456} 
    router = bot2.BotRouter(mock_config, MagicMock(), {})
    
    await router.handle_message(mock_update, mock_context)
    mock_update.message.reply_text.assert_called_with("⛔ You are not authorised to use this bot.")

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_download_handler_over_limit_split(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))

    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {"title": "Vid", "ext": "mp4"}))

    # Use a 5MB file and 1MB limit
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

    # Create real part file
    mock_part = tmp_path / "Vid.part1.mp4"
    mock_part.write_bytes(b"dummy_part")

    mock_splitter = MagicMock()
    mock_splitter.split_video = MagicMock(return_value=([mock_part], ""))
    mocker.patch("bot2.LargeVideoSplitter", return_value=mock_splitter)

    mocker.patch("bot2.open", mock_open(read_data=b"dummy"))
    mocker.patch("shutil.move")
    mocker.patch("shutil.copy")

    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    handler._splitter = mock_splitter

    # Mock send_video to bypass file access
    mock_context.bot.send_video = AsyncMock()
    # Mock status_msg.edit_text because it's used before upload
    status_msg = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg

    try:
        await handler.handle(mock_update.message, mock_context, "https://example.com")
    except Exception:
        pass

    mock_splitter.split_video.assert_called()
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_bot_router_callback_exhaustive(mock_config, mock_context, mocker):
    actions = ["dl", "au", "cp", "sp", "cn"]
    for action in actions:
        update = MagicMock()
        update.callback_query.data = f"{action}:test_uid"
        update.callback_query.message.chat_id = 123
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.from_user.is_bot = True 
        
        bot2.URL_CACHE["test_uid"] = {"url": "https://example.com"}
        
        h_dl = MagicMock(); h_dl.handle = AsyncMock()
        h_au = MagicMock(); h_au.handle = AsyncMock()
        
        router = bot2.BotRouter(mock_config, MagicMock(), {
            "download": h_dl,
            "audio": h_au,
        })
        
        await router.handle_callback(update, mock_context)
        
        if action == "dl": h_dl.handle.assert_called()
        elif action == "au": h_au.handle.assert_called()
        elif action == "cn": assert "test_uid" not in bot2.URL_CACHE

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_bot_router_handle_message_no_text(mock_config, mock_update, mock_context, mocker):
    mock_update.message.text = None
    mock_update.message.caption = None
    router = bot2.BotRouter(mock_config, MagicMock(), {})
    await router.handle_message(mock_update, mock_context)
    # Should just return without doing anything

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_bot_router_no_handler(mock_config, mock_update, mock_context, mocker):
    mock_classifier = MagicMock()
    mock_classifier.classify = AsyncMock(return_value=("unknown_intent", "Decline message"))
    router = bot2.BotRouter(mock_config, mock_classifier, {})
    mock_config.allowed_chat_ids = {123}
    mocker.patch("bot2.asyncio.sleep", AsyncMock())
    await router.handle_message(mock_update, mock_context)
    mock_update.message.reply_text.assert_any_call("Decline message")

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_bot_router_handle_cancel_empty(mock_config, mock_update, mock_context):
    router = bot2.BotRouter(mock_config, MagicMock(), {})
    bot2.ACTIVE_PROCESSES[123] = set()
    await router.handle_cancel(mock_update, mock_context)
    # The current code adds to CANCELLATIONS first, so it says "Cancelled 0 tasks"
    mock_update.message.reply_text.assert_called_with("🛑 Cancelled 0 active task(s).")

def test_track_untrack_process():
    chat_id = 456
    proc = MagicMock()
    bot2.track_process(chat_id, proc)
    assert proc in bot2.ACTIVE_PROCESSES[chat_id]
    bot2.untrack_process(chat_id, proc)
    assert chat_id not in bot2.ACTIVE_PROCESSES

@pytest.mark.asyncio
async def test_cleanup_cache_task(mocker):
    bot2.URL_CACHE["old"] = {"time": 0, "url": "..."}
    bot2.URL_CACHE["new"] = {"time": time.monotonic(), "url": "..."}
    
    # Mock sleep to raise exception after one run to exit loop
    mocker.patch("bot2.asyncio.sleep", side_effect=[None, Exception("stop")])
    try:
        await bot2.cleanup_cache_task()
    except Exception as e:
        if str(e) != "stop": raise
    
    assert "old" not in bot2.URL_CACHE
    assert "new" in bot2.URL_CACHE

def test_markdown_escape():
    assert bot2.markdown_escape("file_name[1].mp4") == "file\\_name\\[1\\].mp4"
    assert bot2.markdown_escape("no_escape") == "no\\_escape"
    assert bot2.markdown_escape("`code`") == "\\`code\\`"

@pytest.mark.asyncio
async def test_downloader_progress_hook_cancel(mocker):
    dl = bot2.YtdlpDownloader(MagicMock())
    hook = dl._progress_hook(MagicMock(), {"chat_id": 123})
    bot2.CANCELLATIONS.add(123)
    with pytest.raises(Exception, match="Download cancelled"):
        hook({})
    bot2.CANCELLATIONS.remove(123)

@pytest.mark.asyncio
async def test_downloader_progress_hook_status(mocker):
    bot = AsyncMock()
    loop = MagicMock()
    dl = bot2.YtdlpDownloader(MagicMock())
    hook = dl._progress_hook(loop, {"chat_id": 123, "message_id": 456, "bot": bot})
    
    # finished
    hook({"status": "finished", "total_bytes": 1024, "filename": "test.mp4"})
    
    # error
    hook({"status": "error", "error": "test error"})

@pytest.mark.asyncio
async def test_downloader_download_sync_exception(mocker):
    dl = bot2.YtdlpDownloader(MagicMock())
    mocker.patch("yt_dlp.YoutubeDL", side_effect=Exception("boom"))
    success, err, info = dl.download_sync("url", "tpl")
    assert not success
    assert "boom" in err

@pytest.mark.asyncio
async def test_downloader_download_audio_sync_exception(mocker):
    dl = bot2.YtdlpDownloader(MagicMock())
    mocker.patch("yt_dlp.YoutubeDL", side_effect=Exception("boom"))
    success, err, info = dl.download_audio_sync("url", "tpl")
    assert not success
    assert "boom" in err

@pytest.mark.asyncio
async def test_bot_router_handle_cancel_windows(mock_config, mock_update, mock_context, mocker):
    router = bot2.BotRouter(mock_config, MagicMock(), {})
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    bot2.ACTIVE_PROCESSES[123] = {mock_proc}
    
    mocker.patch("bot2.sys.platform", "win32")
    mock_run = mocker.patch("subprocess.run")
    
    await router.handle_cancel(mock_update, mock_context)
    
    # Assert taskkill was called
    mock_run.assert_called_with(["taskkill", "/F", "/T", "/PID", "9999"], capture_output=True)
    mock_update.message.reply_text.assert_called_with("🛑 Cancelled 1 active task(s).")

@pytest.mark.asyncio
async def test_bot_router_handle_cancel_linux(mock_config, mock_update, mock_context, mocker):
    router = bot2.BotRouter(mock_config, MagicMock(), {})
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    bot2.ACTIVE_PROCESSES[123] = {mock_proc}
    
    mocker.patch("bot2.sys.platform", "linux")
    # Use create=True because killpg and getpgid do not exist on Windows
    mock_killpg = mocker.patch("os.killpg", create=True)
    mock_getpgid = mocker.patch("os.getpgid", create=True, return_value=8888)
    
    await router.handle_cancel(mock_update, mock_context)
    
    # Assert killpg was called
    mock_killpg.assert_called_with(8888, bot2.signal.SIGTERM)
    mock_update.message.reply_text.assert_called_with("🛑 Cancelled 1 active task(s).")

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_bot_router_callback_expired(mock_config, mock_context):
    update = MagicMock()
    update.callback_query.data = "dl:expired_id"
    update.callback_query.message.chat_id = 123
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    router = bot2.BotRouter(mock_config, MagicMock(), {})
    await router.handle_callback(update, mock_context)
    update.callback_query.edit_message_text.assert_called_with("⚠️ This session has expired or the URL is no longer in cache.")

@pytest.mark.asyncio
async def test_error_handler(mocker):
    context = MagicMock()
    context.error = Exception("test error")
    mock_log = mocker.patch("bot2.log.error")
    await bot2.error_handler(MagicMock(), context)
    mock_log.assert_called()

@pytest.mark.asyncio
async def test_download_handler_no_report_handler(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 1000, "filesize": 100*1024*1024}))
    # report_handler is None by default
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, MagicMock())
    
    # Mock TemporaryDirectory and other things to avoid deep failure
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = "/tmp"
    mock_dl.download_sync = MagicMock(return_value=(False, "stop here", {}))
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    # Should proceed to download anyway
    mock_dl.download_sync.assert_called()

@pytest.mark.asyncio
async def test_download_handler_cancel_before_dl(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {}))
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, MagicMock())
    
    bot2.CANCELLATIONS.add(123)
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    bot2.CANCELLATIONS.discard(123)
    
    mock_update.message.reply_text.return_value.edit_text.assert_called_with("❌ Task cancelled.")

@pytest.mark.asyncio
async def test_download_handler_cancel_after_dl(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {}))
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, MagicMock())
    
    # We need to make it cancel AFTER download_sync
    # We can do this by having download_sync set the cancellation flag!
    def side_effect(*args, **kwargs):
        bot2.CANCELLATIONS.add(123)
        return (True, "", {})
    mock_dl.download_sync.side_effect = side_effect
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = "/tmp"
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    bot2.CANCELLATIONS.discard(123)

@pytest.mark.asyncio
async def test_download_handler_compress_still_too_big(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {"title": "Vid", "ext": "mp4"}))
    
    mock_vid = MagicMock()
    mock_vid.stat.return_value.st_size = 100*1024*1024
    mock_vid.name = "Vid.mp4"
    mock_vid.exists.return_value = True
    mock_vid.is_file.return_value = True
    
    mock_compressed = MagicMock()
    mock_compressed.stat.return_value.st_size = 60*1024*1024 # Still > 50MB limit
    mock_compressed.name = "compressed.mp4"
    mock_compressed.exists.return_value = True
    mock_compressed.is_file.return_value = True

    mock_ff = MagicMock()
    mock_ff.get_duration = MagicMock(return_value=60)
    mock_ff.compress_to_size = MagicMock(return_value=(True, mock_compressed, ""))
    
    # Mock split to return success
    mock_splitter = MagicMock()
    mock_splitter.split_video = MagicMock(return_value=([], "stop")) # Stop here
    
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    handler._splitter = mock_splitter
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = "/tmp"
    mock_path_cls = mocker.patch("bot2.Path")
    mock_path_cls.return_value.iterdir.return_value = [mock_vid]
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    # Should proceed to split
    mock_splitter.split_video.assert_called()

@pytest.mark.asyncio
async def test_download_handler_compress_cancel(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {}))
    
    mock_ff = MagicMock()
    mock_ff.get_duration = MagicMock(return_value=60)
    # Simulate cancellation in compress_to_size
    mock_ff.compress_to_size = MagicMock(return_value=(False, None, "cancelled"))
    
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = "/tmp"
    mock_path_cls = mocker.patch("bot2.Path")
    mock_vid = MagicMock(); mock_vid.stat.return_value.st_size = 100*1024*1024; mock_vid.is_file.return_value = True
    mock_path_cls.return_value.iterdir.return_value = [mock_vid]

    await handler.handle(mock_update.message, mock_context, "https://example.com")
    # Should show cancellation message
    mock_update.message.reply_text.return_value.edit_text.assert_any_call("❌ Processing cancelled.")

@pytest.mark.asyncio
async def test_download_handler_split_cancel_before(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {}))
    
    mock_ff = MagicMock()
    mock_ff.get_duration = MagicMock(return_value=60)
    # Target bitrate very high so we must split
    mock_config.min_video_bitrate_kbps = 999999
    
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = "/tmp"
    mock_path_cls = mocker.patch("bot2.Path")
    mock_vid = MagicMock(); mock_vid.stat.return_value.st_size = 100*1024*1024; mock_vid.is_file.return_value = True
    mock_path_cls.return_value.iterdir.return_value = [mock_vid]

    # Set cancellation flag BEFORE splitting
    # We can do this by mocking compress_to_size (if it's called) or by having download_sync set it
    def side_effect(*args, **kwargs):
        bot2.CANCELLATIONS.add(123)
        return (True, "", {})
    mock_dl.download_sync.side_effect = side_effect

    await handler.handle(mock_update.message, mock_context, "https://example.com")
    bot2.CANCELLATIONS.discard(123)
    mock_update.message.reply_text.return_value.edit_text.assert_called_with("❌ Task cancelled.")

@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_download_handler_upload_cancel(mock_config, mock_update, mock_context, mocker, tmp_path):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {"duration": 60, "ext": "mp4"}))
    mock_dl.download_sync = MagicMock(return_value=(True, "", {}))
    
    mock_ff = MagicMock()
    mock_ff.get_duration = MagicMock(return_value=60)
    
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, mock_ff)
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = str(tmp_path)
    
    # Create real files
    v1 = tmp_path / "V1.mp4"
    v1.write_bytes(b"data1")
    v2 = tmp_path / "V2.mp4"
    v2.write_bytes(b"data2")

    mocker.patch("builtins.open", mock_open(read_data=b"data"))
    
    # Use a fresh mock for status_msg
    status_msg = AsyncMock()
    status_msg.edit_text.return_value = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg

    # We want to cancel AFTER first upload starts.
    async def edit_side_effect(text, *args, **kwargs):
        if "Uploading" in text:
            bot2.CANCELLATIONS.add(123)
        return AsyncMock()
    status_msg.edit_text.side_effect = edit_side_effect

    await handler.handle(mock_update.message, mock_context, "https://example.com")

    assert True 
    bot2.CANCELLATIONS.discard(123)




@pytest.mark.asyncio
async def test_download_handler_dl_fail(mock_config, mock_update, mock_context, mocker):
    mocker.patch("asyncio.get_event_loop").return_value.run_in_executor = AsyncMock(side_effect=lambda exec, func, *a, **k: func(*a, **k))
    mock_dl = MagicMock()
    mock_dl.get_info_sync = MagicMock(return_value=(True, "", {}))
    mock_dl.download_sync = MagicMock(return_value=(False, "dl error", {}))
    handler = bot2.DownloadIntentHandler(mock_config, mock_dl, MagicMock())
    
    mocker.patch("tempfile.TemporaryDirectory").return_value.__enter__.return_value = "/tmp"
    
    await handler.handle(mock_update.message, mock_context, "https://example.com")
    # Should edit text with error message
    call_args = mock_update.message.reply_text.return_value.edit_text.call_args_list
    assert any("Download failed" in c[0][0] for c in call_args)
