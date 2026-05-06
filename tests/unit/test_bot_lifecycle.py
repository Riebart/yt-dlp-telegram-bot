import pytest
from unittest.mock import MagicMock, patch
from bot.utils import error_handler
from main import main

@pytest.mark.asyncio
async def test_error_handler():
    update = MagicMock()
    context = MagicMock()
    context.error = Exception("Test Exception")
    
    # Just ensure it executes without crashing
    await error_handler(update, context)

@patch("main.ApplicationBuilder")
@patch("main.Config")
@patch("main.BotRouter")
@patch("main.FFmpegProcessor")
@patch("main.YtdlpDownloader")
@patch("main.OllamaClassifier")
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
