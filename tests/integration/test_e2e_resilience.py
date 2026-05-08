import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from bot.state import CANCELLATIONS
from bot.router import BotRouter

@pytest.mark.asyncio
async def test_router_unknown_intent(e2e_router, mock_update, mock_context, mocker):
    """Test that an unknown intent from the classifier is handled gracefully."""
    # Mock classifier to return an unknown intent
    mocker.patch.object(e2e_router._classifier, "classify", return_value=("unknown", "I'm not sure how to help with that unknown request."))
    
    mock_update.message.text = "Some random text that shouldn't be a command"
    
    # Mock status_msg to prevent actual delete/sleep delays and simplify tracking
    status_msg_mock = AsyncMock()
    mock_update.message.reply_text.return_value = status_msg_mock
    
    await e2e_router.handle_message(mock_update, mock_context)
    
    # On unknown intent, router does:
    # 1. reply_text("Classifying")
    # 2. status_msg.edit_text("model -> unknown")
    # 3. status_msg.delete()
    # 4. reply_text(decline_reply)
    
    # Check that reply_text was called with the decline message
    # We check all calls to reply_text on the message object
    replies = [call.args[0] for call in mock_update.message.reply_text.call_args_list]
    assert any("I'm not sure" in text for text in replies)

@pytest.mark.asyncio
async def test_router_classifier_crash(e2e_router, mock_update, mock_context, mocker):
    """Test that a crash in the classifier is handled without crashing the bot."""
    # Mock classifier to raise an exception
    mocker.patch.object(e2e_router._classifier, "classify", side_effect=Exception("LLM API Down"))
    
    mock_update.message.text = "download http://example.com/video.mp4"
    
    # Custom mock that actually stores text in a list when edit_text is called
    edit_calls = []
    class StatusMsgMock:
        async def edit_text(self, text, *args, **kwargs):
            edit_calls.append(text)
        async def delete(self):
            pass
            
    mock_update.message.reply_text = AsyncMock(return_value=StatusMsgMock())
    
    await e2e_router.handle_message(mock_update, mock_context)
    
    # Verify that the bot sends an error message to the user via status_msg.edit_text
    assert any("Sorry" in text for text in edit_calls)
    assert any("error" in text.lower() for text in edit_calls)
