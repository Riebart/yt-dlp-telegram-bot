import pytest
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import bot2
import yt_dlp

def test_ytdlp_logger(caplog):
    logger = bot2._YtdlpLogger()
    with caplog.at_level("INFO"):
        logger.debug("[download] working")
        logger.debug("other debug")
        logger.info("some info")
        logger.warning("a warning")
        logger.error("an error")
    
    assert "[download] working" in caplog.text
    assert "some info" in caplog.text
    assert "a warning" in caplog.text
    assert "an error" in caplog.text

@pytest.mark.asyncio
async def test_ytdlp_progress_hook(mock_downloader, mocker):
    loop = asyncio.get_running_loop()
    bot = MagicMock()
    ctx = {"chat_id": 123, "message_id": 456, "bot": bot}
    
    hook = mock_downloader._progress_hook(loop, ctx)
    
    # Test downloading status
    start_time = time.time()
    mocker.patch("time.monotonic", side_effect=lambda: time.time() - start_time + 100)
    
    hook({"status": "downloading", "_percent_str": "50%", "_speed_str": "1MB/s", "_eta_str": "10s"})
    # Wait a bit for the async task to be scheduled
    await asyncio.sleep(0.01)
    bot.edit_message_text.assert_called()

    # Test cancellation
    bot2.CANCELLATIONS.add(123)
    with pytest.raises(Exception, match="Download cancelled"):
        hook({"status": "downloading"})
    bot2.CANCELLATIONS.clear()

    # Test finished status
    hook({"status": "finished", "filename": "vid.mp4", "total_bytes": 1024*1024})
    
    # Test error status
    hook({"status": "error", "error": "failed"})

def test_ytdlp_download_sync_success(mock_downloader, mocker):
    mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
    mock_ydl.return_value.__enter__.return_value.extract_info.return_value = {"title": "test"}
    
    success, err, info = mock_downloader.download_sync("url", "tpl", loop=MagicMock())
    assert success
    assert info["title"] == "test"

def test_ytdlp_download_sync_fail(mock_downloader, mocker):
    mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
    mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = yt_dlp.utils.DownloadError("fail")
    
    success, err, info = mock_downloader.download_sync("url", "tpl", loop=MagicMock())
    assert not success
    assert "fail" in err

def test_ytdlp_download_sync_exception(mock_downloader, mocker):
    mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
    mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception("unexpected")
    
    success, err, info = mock_downloader.download_sync("url", "tpl", loop=MagicMock())
    assert not success
    assert "unexpected" in err

def test_ytdlp_get_info_sync(mock_downloader, mocker):
    mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
    mock_ydl.return_value.__enter__.return_value.extract_info.return_value = {"id": "123"}
    
    success, err, info = mock_downloader.get_info_sync("url")
    assert success
    assert info["id"] == "123"
    
    mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception("err")
    success, err, info = mock_downloader.get_info_sync("url")
    assert not success

@pytest.mark.asyncio
async def test_ytdlp_progress_hook_various_branches(mock_downloader, mocker):
    loop = asyncio.get_running_loop()
    
    # Test ctx is None
    hook = mock_downloader._progress_hook(loop, None)
    hook({"status": "downloading", "_percent_str": "50%"}) # Should not crash
    
    # Test ctx is not a dict
    hook = mock_downloader._progress_hook(loop, "not a dict")
    hook({"status": "downloading", "_percent_str": "50%"}) # Should not crash
    
    # Test ctx missing 'bot'
    hook = mock_downloader._progress_hook(loop, {"chat_id": 1})
    hook({"status": "downloading", "_percent_str": "50%"}) # Should not crash

    # Test ctx with 'bot' but missing chat_id/message_id
    bot = MagicMock()
    hook = mock_downloader._progress_hook(loop, {"bot": bot})
    hook({"status": "downloading", "_percent_str": "50%"}) # Should not crash
    
    # Test status='finished' with no total_bytes
    hook({"status": "finished", "filename": "f.mp4"}) # Uses estimate/default

def test_ytdlp_download_audio_sync_fail(mock_downloader, mocker):
    mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
    mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = yt_dlp.utils.DownloadError("audio fail")
    
    success, err, info = mock_downloader.download_audio_sync("url", "tpl", loop=MagicMock())
    assert not success
    assert "audio fail" in err

def test_ytdlp_download_audio_sync_exception(mock_downloader, mocker):
    mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
    mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception("audio unexpected")
    
    success, err, info = mock_downloader.download_audio_sync("url", "tpl", loop=MagicMock())
    assert not success
    assert "audio unexpected" in err
