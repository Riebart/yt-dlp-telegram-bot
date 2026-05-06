import pytest
from unittest.mock import MagicMock, patch
from bot2 import error_handler, main

@pytest.mark.asyncio
async def test_error_handler():
    update = MagicMock()
    context = MagicMock()
    context.error = Exception("Test Exception")
    
    # Just ensure it executes without crashing
    await error_handler(update, context)

@patch("bot2.ApplicationBuilder")
@patch("bot2.Config")
@patch("bot2.BotRouter")
@patch("bot2.FFmpegProcessor")
@patch("bot2.YtdlpDownloader")
@patch("bot2.OllamaClassifier")
def test_main_startup(
    mock_classifier, mock_downloader, mock_ffmpeg, mock_router, mock_config, mock_app_builder
):
    # Setup
    mock_app = MagicMock()
    mock_app_builder.return_value.token.return_value.request.return_value.build.return_value = mock_app
    
    # Run
    main()
    
    # Assert
    mock_app.run_polling.assert_called_once()
