import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from bot.state import CANCELLATIONS, URL_CACHE
from bot.router import BotRouter

@pytest.mark.asyncio
async def test_router_handle_cancel_no_tasks(e2e_router, mock_update, mock_context):
    """Test /cancel when no tasks are active for the user."""
    mock_update.message.chat_id = 12345
    
    await e2e_router.handle_cancel(mock_update, mock_context)
    
    mock_update.message.reply_text.assert_called_with("ℹ️ No active tasks to cancel.")

@pytest.mark.asyncio
async def test_router_handle_cancel_with_tasks(e2e_router, mock_update, mock_context, mocker):
    """Test /cancel when tasks are active, ensuring processes are terminated."""
    mock_update.message.chat_id = 12345
    
    # Mock a process in ACTIVE_PROCESSES
    from bot.state import ACTIVE_PROCESSES, USER_JOBS
    mock_proc = MagicMock()
    mock_proc.pid = 999
    job_id = "test_job"
    ACTIVE_PROCESSES[job_id] = {mock_proc}
    USER_JOBS[12345] = {job_id}
    
    # Patch terminate_process_group to avoid actual system calls
    mock_terminate = mocker.patch("bot.router.terminate_process_group")
    
    await e2e_router.handle_cancel(mock_update, mock_context)
    
    assert job_id not in ACTIVE_PROCESSES
    mock_terminate.assert_called_once_with(mock_proc.pid)
    mock_terminate.assert_called_once_with(999)
    mock_update.message.reply_text.assert_called_with("🛑 Cancelled 1 active task(s).")
    
    # Cleanup
    ACTIVE_PROCESSES.clear()

@pytest.mark.asyncio
async def test_router_handle_callback_download(e2e_router, mock_update, mock_context, mocker):
    """Test callback for 'download' action."""
    # Setup callback query
    mock_query = MagicMock()
    mock_query.answer = AsyncMock()
    mock_query.edit_message_text = AsyncMock()
    mock_query.message.chat_id = 12345
    mock_query.data = "dl:u1"
    mock_update.callback_query = mock_query
    
    # Setup cache
    URL_CACHE["u1"] = {"url": "http://example.com/video.mp4"}
    
    # Mock the download handler to verify it's called
    mock_handler = AsyncMock()
    e2e_router._handlers["download"] = mock_handler
    
    await e2e_router.handle_callback(mock_update, mock_context)
    
    # Verify callback handling
    mock_query.answer.assert_called_once()
    mock_query.edit_message_text.assert_called()
    # Check if the handler was called with the URL from cache
    mock_handler.handle.assert_called_once()
    args, _ = mock_handler.handle.call_args
    assert args[2] == "http://example.com/video.mp4"
    
    # Cleanup
    URL_CACHE.clear()

@pytest.mark.asyncio
async def test_router_handle_callback_cancel(e2e_router, mock_update, mock_context, mocker):
    """Test callback for 'cancel' action."""
    mock_query = MagicMock()
    mock_query.answer = AsyncMock()
    mock_query.edit_message_text = AsyncMock()
    mock_query.message.chat_id = 12345
    job_id = "u1"
    mock_query.data = f"cn:{job_id}"
    mock_update.callback_query = mock_query

    URL_CACHE[job_id] = {"url": "http://example.com/video.mp4"}

    # Mock a process for this job
    from bot.state import ACTIVE_PROCESSES
    mock_proc = MagicMock()
    mock_proc.pid = 999
    ACTIVE_PROCESSES[job_id] = {mock_proc}

    mock_terminate = mocker.patch("bot.router.terminate_process_group")

    await e2e_router.handle_callback(mock_update, mock_context)

    mock_query.edit_message_text.assert_called_with("❌ Action cancelled.")
    assert job_id not in ACTIVE_PROCESSES
    mock_terminate.assert_called_once_with(mock_proc.pid)

    
    # Cleanup
    URL_CACHE.clear()

@pytest.mark.asyncio
async def test_router_handle_callback_expired(e2e_router, mock_update, mock_context):
    """Test callback when the session has expired (URL not in cache)."""
    mock_query = MagicMock()
    mock_query.answer = AsyncMock()
    mock_query.edit_message_text = AsyncMock()
    mock_query.message.chat_id = 12345
    mock_query.data = "dl:expired_u1"
    mock_update.callback_query = mock_query
    
    # URL_CACHE is empty
    URL_CACHE.clear()
    
    await e2e_router.handle_callback(mock_update, mock_context)
    
    mock_query.edit_message_text.assert_called_with("⚠️ This session has expired or the URL is no longer in cache.")
