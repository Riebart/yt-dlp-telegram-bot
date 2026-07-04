import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bot.router import BotRouter
from bot.state import CANCELLATIONS, ACTIVE_PROCESSES, USER_JOBS, URL_CACHE
from telegram import Update, Message


@pytest.fixture
def mock_router():
    cfg = MagicMock()
    cfg.allowed_chat_ids = None
    cfg.ollama_model = "test-model"
    cfg.ollama_timeout = 10
    classifier = MagicMock()
    classifier.classify = AsyncMock(return_value=("download", "Decline"))
    handlers = {"download": MagicMock()}
    handlers["download"].handle = AsyncMock()
    return BotRouter(cfg, classifier, handlers)

@pytest.mark.asyncio
async def test_parallel_job_cancellation_isolation(mock_router):
    """
    Verify that cancelling one job does not cancel another job 
    for the same user.
    """
    CANCELLATIONS.clear()
    ACTIVE_PROCESSES.clear()
    USER_JOBS.clear()
    
    chat_id = 123
    job_a = "job_A"
    job_b = "job_B"
    
    USER_JOBS[chat_id] = {job_a, job_b}
    ACTIVE_PROCESSES[job_a] = {MagicMock(pid=101)}
    ACTIVE_PROCESSES[job_b] = {MagicMock(pid=102)}
    
    # Trigger cancellation for Job B only
    update = MagicMock(spec=Update)
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.chat_id = chat_id
    query.data = f"cn:{job_b}"
    update.callback_query = query
    
    # Populate URL_CACHE so handle_callback doesn't return early
    URL_CACHE[job_b] = {"url": "https://example.com/video", "time": time.monotonic()}
    
    with patch('bot.router.terminate_process_group') as mock_terminate:
        await mock_router.handle_callback(update, MagicMock())
        
        # Job B should be cancelled and its process killed
        assert job_b in CANCELLATIONS
        mock_terminate.assert_called_once_with(102)
        
        # Job A should remain active
        assert job_a not in CANCELLATIONS
        assert job_a in ACTIVE_PROCESSES
        assert len(ACTIVE_PROCESSES[job_a]) == 1

@pytest.mark.asyncio
async def test_global_cancel_all_jobs(mock_router):
    """
    Verify that /cancel terminates all jobs associated with a user.
    """
    CANCELLATIONS.clear()
    ACTIVE_PROCESSES.clear()
    USER_JOBS.clear()
    
    chat_id = 123
    job_a = "job_A"
    job_b = "job_B"
    
    USER_JOBS[chat_id] = {job_a, job_b}
    ACTIVE_PROCESSES[job_a] = {MagicMock(pid=101)}
    ACTIVE_PROCESSES[job_b] = {MagicMock(pid=102)}
    
    update = MagicMock(spec=Update)
    msg = MagicMock(spec=Message)
    msg.chat_id = chat_id
    update.message = msg
    
    with patch('bot.router.terminate_process_group') as mock_terminate:
        await mock_router.handle_cancel(update, MagicMock())
        
        # Both jobs should be flagged for cancellation
        assert job_a in CANCELLATIONS
        assert job_b in CANCELLATIONS
        # Both processes should have been killed
        assert mock_terminate.call_count == 2
        # Both should be removed from active processes
        assert job_a not in ACTIVE_PROCESSES
        assert job_b not in ACTIVE_PROCESSES
