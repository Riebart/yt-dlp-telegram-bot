import pytest
import json
import asyncio
from unittest.mock import MagicMock, patch
import bot2

@pytest.mark.asyncio
async def test_classifier_success(mock_classifier, mocker):
    """Test successful classification."""
    mock_response = {
        "message": {
            "content": '{"intent": "audio"}'
        }
    }
    mock_client = mocker.patch("ollama.Client")
    mock_client.return_value.chat.return_value = mock_response

    intent, reply = await mock_classifier.classify("some text", 10)
    assert intent == "audio"
    assert reply == "Sorry, I can't handle that message." # Default if not in JSON

@pytest.mark.asyncio
async def test_classifier_markdown_json(mock_classifier, mocker):
    """Test classification with markdown code fences."""
    mock_response = {
        "message": {
            "content": '```json\n{"intent": "download", "reply": "ok"}\n```'
        }
    }
    mock_client = mocker.patch("ollama.Client")
    mock_client.return_value.chat.return_value = mock_response

    intent, reply = await mock_classifier.classify("some text", 10)
    assert intent == "download"
    assert reply == "ok"

@pytest.mark.asyncio
async def test_classifier_invalid_json(mock_classifier, mocker):
    """Test classification with invalid JSON (fails open to download)."""
    mock_response = {
        "message": {
            "content": "not json"
        }
    }
    mock_client = mocker.patch("ollama.Client")
    mock_client.return_value.chat.return_value = mock_response

    intent, reply = await mock_classifier.classify("some text", 10)
    assert intent == "download"
    assert reply == ""

@pytest.mark.asyncio
async def test_classifier_timeout(mock_classifier, mocker):
    """Test classification timeout (fails open to download)."""
    mock_client = mocker.patch("ollama.Client")
    # Simulate timeout by making classify_sync take too long
    def slow_classify(*args, **kwargs):
        import time
        time.sleep(0.5)
        return "audio", ""
    
    mock_classifier.classify_sync = slow_classify
    
    intent, reply = await mock_classifier.classify("some text", 0.1)
    assert intent == "download"
    assert reply == ""

@pytest.mark.asyncio
async def test_classifier_exception(mock_classifier, mocker):
    """Test classification exception (fails open to download)."""
    mock_client = mocker.patch("ollama.Client")
    mock_client.return_value.chat.side_effect = Exception("Ollama error")

    intent, reply = await mock_classifier.classify("some text", 10)
    assert intent == "download"
    assert reply == ""

def test_classifier_sync_unknown_intent(mock_classifier, mocker):
    """Test sync classification with unknown intent field."""
    mock_response = {
        "message": {
            "content": '{"something_else": "val"}'
        }
    }
    mock_client = mocker.patch("ollama.Client")
    mock_client.return_value.chat.return_value = mock_response

    intent, reply = mock_classifier.classify_sync("text")
    assert intent == "unknown"
    assert reply == "Sorry, I can't handle that message."
