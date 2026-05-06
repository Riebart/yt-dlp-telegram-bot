#!/usr/bin/env python3
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest

from bot.config import Config
from bot.classifier import OllamaClassifier
from bot.downloader import YtdlpDownloader
from bot.processor import FFmpegProcessor
from bot.router import BotRouter
from bot.handlers.download import DownloadIntentHandler
from bot.handlers.audio import AudioIntentHandler
from bot.handlers.report import ReportSizeIntentHandler
from bot.state import cleanup_cache_task
from bot.utils import error_handler

load_dotenv()

def _setup_logging(level: str) -> logging.Logger:
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, stream=sys.stdout)
    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setLevel(logging.WARNING)
    stderr_h.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logging.getLogger().addHandler(stderr_h)
    for noisy in ("httpx", "httpcore", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("bot")

def main() -> None:
    cfg = Config()
    log = _setup_logging(cfg.log_level)
    cfg.log_startup()

    # Infrastructure
    ffmpeg     = FFmpegProcessor(cfg)
    downloader = YtdlpDownloader(cfg)
    classifier = OllamaClassifier(cfg)

    # Handlers
    reporter = ReportSizeIntentHandler(cfg, downloader)
    handlers = {
        "download":           DownloadIntentHandler(cfg, downloader, ffmpeg, reporter),
        "large_video_split":  DownloadIntentHandler(cfg, downloader, ffmpeg, reporter),
        "large_video_compress":  DownloadIntentHandler(cfg, downloader, ffmpeg, reporter),
        "large_video_auto":   DownloadIntentHandler(cfg, downloader, ffmpeg, reporter),
        "report_size":        reporter,
        "audio":              AudioIntentHandler(cfg, downloader, reporter)
    }

    router = BotRouter(cfg, classifier, handlers)

    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60,
        write_timeout=180,
        connect_timeout=30,
    )
    app = ApplicationBuilder().token(cfg.bot_token).request(request).build()

    # Register handlers
    app.add_handler(CommandHandler("cancel", router.handle_cancel))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            router.handle_message,
        )
    )
    app.add_handler(CallbackQueryHandler(router.handle_callback))
    app.add_error_handler(error_handler)

    # Start background tasks
    asyncio.ensure_future(cleanup_cache_task())

    log.info("Polling started — waiting for messages.")
    app.run_polling(drop_pending_updates=True)
    log.info("Bot shut down cleanly.")

if __name__ == "__main__":
    main()
