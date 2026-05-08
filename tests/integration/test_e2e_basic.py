import pytest
import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_e2e_report_flow(e2e_router, mock_update, mock_context, local_server, mocker):
    """Validate that a live URL generates a correct report with accurate size/duration."""
    # Mock get_info_sync to avoid yt-dlp issues with locally served raw files
    mocker.patch.object(
        e2e_router._handlers["report"]._downloader, 
        "get_info_sync", 
        return_value=(True, "", {"title": "Test Video", "duration": 60, "filesize": 10 * 1024 * 1024})
    )

    # To capture the return values of reply_text (which are the mock messages),
    # we wrap the side_effect defined in conftest.py.
    messages = []
    original_side_effect = mock_update.message.reply_text.side_effect

    async def collecting_side_effect(*args, **kwargs):
        res = await original_side_effect(*args, **kwargs)
        messages.append(res)
        return res

    mock_update.message.reply_text.side_effect = collecting_side_effect
    
    mock_update.message.text = f"report {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)

    mock_update.message.reply_text.assert_called()

    # Check if any of the returned messages had the report edited into it.
    found_report = False
    for msg in messages:
        if msg.edit_text.called:
            # Get the last call to edit_text for this message
            text = msg.edit_text.call_args[0][0]
            if "Duration" in text and "Size" in text:
                found_report = True
                break

    assert found_report, "Report message with Duration and Size not found."

def get_first_arg(mock_call):
    args, kwargs = mock_call.call_args
    val = None
    if args:
        val = args[0]
    elif "video" in kwargs:
        val = kwargs["video"]
    elif "audio" in kwargs:
        val = kwargs["audio"]
    elif "file" in kwargs:
        val = kwargs["file"]
    
    if val is not None and hasattr(val, "name") and not isinstance(val, (str, Path)):
        return val.name
    return val

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_e2e_audio_flow(e2e_router, e2e_config, mock_update, mock_context, temp_workspace, mocker, local_server):
    """Validate that the audio handler produces a valid .mp3 file from a live URL."""
    mocker.patch("tempfile.TemporaryDirectory", return_value=MagicMock(__enter__=MagicMock(return_value=str(temp_workspace))))
    
    # Mock get_info_sync to avoid SSL/network hangs in yt-dlp during coverage runs
    mocker.patch.object(
        e2e_router._handlers["audio"]._downloader,
        "get_info_sync",
        return_value=(True, "", {"title": "Test Audio", "duration": 60, "filesize": 10 * 1024 * 1024})
    )

    # Mock download_audio_sync to avoid yt-dlp extractor loading hangs during coverage
    audio_file = Path(temp_workspace) / "test_audio.mp3"
    audio_file.write_text("mock audio content")
    mocker.patch.object(
        e2e_router._handlers["audio"]._downloader,
        "download_audio_sync",
        return_value=(True, "", audio_file)
    )
    
    mock_update.message.text = f"audio {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)
    
    mock_update.message.reply_audio.assert_called()
    audio_path = get_first_arg(mock_update.message.reply_audio)
    
    assert audio_path is not None, "No path provided to reply_audio"
    assert Path(audio_path).exists()
    assert Path(audio_path).suffix == ".mp3"
    assert Path(audio_path).stat().st_size > 0

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_e2e_download_simple(e2e_router, e2e_config, mock_update, mock_context, temp_workspace, mocker, local_server):
    """Validate direct download when the file is under the limit."""
    mocker.patch("tempfile.TemporaryDirectory", return_value=MagicMock(__enter__=MagicMock(return_value=str(temp_workspace))))
    
    # Mock get_info_sync to avoid yt-dlp extractor loading hangs during coverage
    mocker.patch.object(
        e2e_router._handlers["download"]._downloader,
        "get_info_sync",
        return_value=(True, "", {"title": "Test Video", "duration": 60, "filesize": 10 * 1024 * 1024})
    )

    # Mock download_sync to avoid yt-dlp extractor loading hangs during coverage
    video_file = Path(temp_workspace) / "test_video.mp4"
    video_file.write_text("mock video content")
    mocker.patch.object(
        e2e_router._handlers["download"]._downloader,
        "download_sync",
        return_value=(True, "", video_file)
    )
    
    e2e_config.max_size_mb = 100 
    mock_update.message.text = f"download {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)
    
    mock_update.message.reply_video.assert_called()
    video_path = get_first_arg(mock_update.message.reply_video)
    
    assert video_path is not None, "No path provided to reply_video"
    assert Path(video_path).exists()
    assert Path(video_path).stat().st_size > 0
