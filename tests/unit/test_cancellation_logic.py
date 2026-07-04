import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from bot.router import BotRouter
from bot.state import CANCELLATIONS, ACTIVE_PROCESSES, USER_JOBS
from telegram import Update, Message, Chat

@pytest.fixture
def mock_router():
    # Minimal mocks for dependencies
    cfg = MagicMock()
    cfg.allowed_chat_ids = None
    cfg.ollama_model = "test-model"
    cfg.ollama_timeout = 10
    
    classifier = MagicMock()
    classifier.classify = AsyncMock(return_value=("download", "Decline message"))
    
    handlers = {
        "download": MagicMock()
    }
    handlers["download"].handle = AsyncMock()
    
    return BotRouter(cfg, classifier, handlers)

@pytest.mark.asyncio
async def test_cross_chat_cancellation_interference(mock_router):
    """
    Reproduction test: A message from User B should NOT clear the 
    cancellation flag for User A.
    """
    CANCELLATIONS.clear()
    user_a_id = 123
    user_b_id = 456
    
    # User A requests cancellation
    CANCELLATIONS.add(user_a_id)
    assert user_a_id in CANCELLATIONS
    
    # Mock an update from User B
    update_b = MagicMock(spec=Update)
    msg_b = MagicMock(spec=Message)
    msg_b.chat_id = user_b_id
    msg_b.message_id = 1
    msg_b.from_user.username = "user_b"
    msg_b.text = "Hello bot"
    update_b.message = msg_b
    
    context = MagicMock()
    
    # Simulate User B sending a message
    await mock_router.handle_message(update_b, context)
    
    # User A's cancellation should still be active
    assert user_a_id in CANCELLATIONS, "User B's message incorrectly cleared User A's cancellation flag"

@pytest.mark.asyncio
async def test_active_processes_cleaned_after_cancel(mock_router):
    """
    Reproduction test: handle_cancel should remove processes from 
    ACTIVE_PROCESSES after terminating them.
    """
    ACTIVE_PROCESSES.clear()
    USER_JOBS.clear()
    chat_id = 789
    job_id = "test_job"
    
    # Simulate an active process
    mock_proc = MagicMock()
    mock_proc.pid = 1001
    ACTIVE_PROCESSES[job_id] = {mock_proc}
    USER_JOBS[chat_id] = {job_id}
    
    # Mock Update for /cancel
    update = MagicMock(spec=Update)
    msg = MagicMock(spec=Message)
    msg.chat_id = chat_id
    update.message = msg
    
    context = MagicMock()
    
    with patch('bot.router.terminate_process_group') as mock_terminate:
        await mock_router.handle_cancel(update, context)
        
        # Verify termination was called
        mock_terminate.assert_called_once_with(mock_proc.pid)
        
        # Verify the job is removed from state
        assert job_id not in ACTIVE_PROCESSES, "Job processes were not removed from ACTIVE_PROCESSES after cancellation"
