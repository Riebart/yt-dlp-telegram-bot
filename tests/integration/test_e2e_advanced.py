import pytest
import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from bot.state import CANCELLATIONS, URL_CACHE, ACTIVE_PROCESSES
from tests.integration.test_e2e_basic import get_first_arg

@pytest.mark.asyncio
@pytest.mark.timeout(35)
async def test_e2e_download_compress_success(e2e_router, e2e_config, mock_update, mock_context, temp_workspace, mocker, local_server):
    """Test successful compression when target bitrate is above floor."""
    mocker.patch("tempfile.TemporaryDirectory", return_value=MagicMock(__enter__=MagicMock(return_value=str(temp_workspace))))

    # Set max_size_mb slightly below typical sample size (~1.5MB)
    e2e_config.max_size_mb = 1.0
    e2e_config.compress_mb = 0.8
    e2e_config.min_video_bitrate_kbps = 10

    mock_update.message.text = f"download {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)

    mock_update.message.reply_video.assert_called()
    video_path = get_first_arg(mock_update.message.reply_video)

    assert video_path is not None
    assert Path(video_path).exists()
    assert Path(video_path).stat().st_size <= 1.0 * 1024 * 1024

@pytest.mark.asyncio
@pytest.mark.timeout(35)
async def test_e2e_download_compress_floor(e2e_router, e2e_config, mock_update, mock_context, temp_workspace, mocker, local_server):
    """Test pivot to splitting when compression would hit the quality floor."""
    mocker.patch("tempfile.TemporaryDirectory", return_value=MagicMock(__enter__=MagicMock(return_value=str(temp_workspace))))

    # Set max_size_mb and min_video_bitrate_kbps to force the floor
    e2e_config.max_size_mb = 0.7
    e2e_config.compress_mb = 0.6
    e2e_config.min_video_bitrate_kbps = 5000 # High floor to force split

    mock_update.message.text = f"download {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)

    # Should result in multiple parts being uploaded (because it pivoted to splitting)
    assert mock_update.message.reply_video.call_count > 1

    # Verify the first part exists
    first_video_path = get_first_arg(mock_update.message.reply_video)
    assert first_video_path is not None
    assert Path(first_video_path).exists()

@pytest.mark.asyncio
@pytest.mark.timeout(35)
async def test_e2e_download_split_simple(e2e_router, e2e_config, mock_update, mock_context, temp_workspace, mocker, local_server):
    """Test simple splitting (without preceding compression)."""
    mocker.patch("tempfile.TemporaryDirectory", return_value=MagicMock(__enter__=MagicMock(return_value=str(temp_workspace))))

    # Force split by setting min_video_bitrate very high and max_size moderately low
    e2e_config.max_size_mb = 0.5
    e2e_config.min_video_bitrate_kbps = 100000

    mock_update.message.text = f"download {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)

    assert mock_update.message.reply_video.call_count > 1

@pytest.mark.asyncio
@pytest.mark.timeout(45)
async def test_e2e_cancel_download(e2e_router, e2e_config, mock_update, mock_context, mocker, local_server):
    """Test cancellation during the download phase."""
    # Collect all message mocks created via reply_text
    messages = []
    original_side_effect = mock_update.message.reply_text.side_effect

    async def collecting_side_effect(*args, **kwargs):
        res = await original_side_effect(*args, **kwargs)
        messages.append(res)
        return res

    mock_update.message.reply_text.side_effect = collecting_side_effect
    
    original_download = e2e_router._handlers["download"]._downloader.download_sync

    async def mocked_download(*args, **kwargs):
        from bot.state import CANCELLATIONS
        CANCELLATIONS.add(mock_update.effective_chat.id)
        await asyncio.sleep(1)
        return original_download(*args, **kwargs)

    mocker.patch("asyncio.get_running_loop").return_value.run_in_executor = AsyncMock(
        side_effect=lambda exec, func, *a, **k: (
            CANCELLATIONS.add(mock_update.effective_chat.id),
            func(*a, **k)
        )[1]
    )

    mock_update.message.text = f"download {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)

    # Check main message and all status messages
    all_edit_mocks = [mock_update.message] + messages
    cancelled_found = False
    for msg in all_edit_mocks:
        if hasattr(msg, 'edit_text') and msg.edit_text.called:
            for call in msg.edit_text.call_args_list:
                if call.args and isinstance(call.args[0], str) and "cancelled" in call.args[0].lower():
                    cancelled_found = True
                    break
        if cancelled_found: break
        
    assert cancelled_found, "Cancellation message not found in any edit_text call"

@pytest.mark.asyncio
@pytest.mark.timeout(45)
async def test_e2e_cancel_compress(e2e_router, e2e_config, mock_update, mock_context, temp_workspace, mocker, local_server):
    """Test cancellation during the compression phase."""
    messages = []
    original_side_effect = mock_update.message.reply_text.side_effect

    async def collecting_side_effect(*args, **kwargs):
        res = await original_side_effect(*args, **kwargs)
        messages.append(res)
        return res

    mock_update.message.reply_text.side_effect = collecting_side_effect
    
    mocker.patch("tempfile.TemporaryDirectory", return_value=MagicMock(__enter__=MagicMock(return_value=str(temp_workspace))))

    # Force compression
    e2e_config.max_size_mb = 0.1
    e2e_config.min_video_bitrate_kbps = 1

    # Capture the original method to avoid recursion
    original_compress = e2e_router._handlers["download"]._ffmpeg.compress_to_size
    
    def side_effect_compress(*args, **kwargs):
        from bot.state import CANCELLATIONS
        CANCELLATIONS.add(mock_update.effective_chat.id)
        return original_compress(*args, **kwargs)

    mocker.patch.object(e2e_router._handlers["download"]._ffmpeg, "compress_to_size", side_effect=side_effect_compress)

    mock_update.message.text = f"download {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)

    all_edit_mocks = [mock_update.message] + messages
    cancelled_found = False
    for msg in all_edit_mocks:
        if hasattr(msg, 'edit_text') and msg.edit_text.called:
            for call in msg.edit_text.call_args_list:
                if call.args and isinstance(call.args[0], str) and "cancelled" in call.args[0].lower():
                    cancelled_found = True
                    break
        if cancelled_found: break
        
    assert cancelled_found, "Cancellation message not found in any edit_text call"

@pytest.mark.asyncio
@pytest.mark.timeout(45)
async def test_e2e_cancel_upload(e2e_router, e2e_config, mock_update, mock_context, temp_workspace, mocker, local_server):
    """Test cancellation during the upload phase of multiple parts."""
    messages = []
    original_side_effect = mock_update.message.reply_text.side_effect

    async def collecting_side_effect(*args, **kwargs):
        res = await original_side_effect(*args, **kwargs)
        messages.append(res)
        return res

    mock_update.message.reply_text.side_effect = collecting_side_effect
    
    mocker.patch("tempfile.TemporaryDirectory", return_value=MagicMock(__enter__=MagicMock(return_value=str(temp_workspace))))

    # Force splitting
    e2e_config.max_size_mb = 0.01
    e2e_config.min_video_bitrate_kbps = 10000

    # Mock upload_media to trigger cancellation after the first part
    call_count = 0
    async def side_effect_upload(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            from bot.state import CANCELLATIONS
            CANCELLATIONS.add(mock_update.effective_chat.id)
        return MagicMock()

    mocker.patch.object(e2e_router._handlers["download"], "upload_media", side_effect=side_effect_upload)

    mock_update.message.text = f"download {local_server}"
    await e2e_router.handle_message(mock_update, mock_context)

    assert e2e_router._handlers["download"].upload_media.call_count == 1

    all_edit_mocks = [mock_update.message] + messages
    cancelled_found = False
    for msg in all_edit_mocks:
        if hasattr(msg, 'edit_text') and msg.edit_text.called:
            for call in msg.edit_text.call_args_list:
                if call.args and isinstance(call.args[0], str) and "cancelled" in call.args[0].lower():
                    cancelled_found = True
                    break
        if cancelled_found: break
        
    assert cancelled_found, "Cancellation message not found in any edit_text call"
