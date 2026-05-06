import logging
import uuid
import time
from typing import Any
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot.handlers.base import BaseIntentHandler
from bot.config import Config
from bot.state import URL_CACHE
from bot.utils import markdown_escape

log = logging.getLogger("bot.handlers.report")

class ReportSizeIntentHandler(BaseIntentHandler):
    """
    Handles the 'report_size' intent:
      1. Extract URL
      2. Fetch metadata via yt-dlp (simulate)
      3. Show info (size, duration)
      4. Offer interactive buttons for next steps
    """

    def __init__(self, cfg: Config, downloader: Any) -> None:
        self._cfg        = cfg
        self._downloader = downloader
        self._log        = log

    async def handle(self, message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        import re
        URL_RE = re.compile(r'''https?://[^\s<>"'{}|\^`\[\]]+''')
        urls = URL_RE.findall(text)
        if not urls:
            await message.reply_text("⚠️ No URL found.")
            return

        url = urls[0]
        status_msg = await message.reply_text(f"🔍 Fetching info for `{url}`...", parse_mode="Markdown")
        await self.send_report(status_msg, url)

    async def send_report(self, status_msg, url: str) -> None:
        """Shared logic to fetch info and show the interactive report."""
        import asyncio
        success, err, info = await asyncio.get_event_loop().run_in_executor(
            None, self._downloader.get_info_sync, url
        )

        if not success:
            await status_msg.edit_text(f"❌ Could not fetch info: {err}")
            return

        title = info.get("title", "Unknown Title")
        duration = info.get("duration", 0)
        size_bytes = info.get("filesize") or info.get("filesize_approx") or 0

        duration_str = f"{int(duration // 60)}m {int(duration % 60)}s" if duration else "Unknown"
        size_str = f"{size_bytes / 1_048_576:.1f} MiB" if size_bytes else "Unknown"

        recommendation = ""
        if duration and size_bytes:
            target_bits = self._cfg.compress_mb * 1024 * 1024 * 8
            audio_bits = self._cfg.audio_bps * duration
            video_bits = target_bits - audio_bits
            required_v_kbps = int(video_bits / duration / 1000)

            if size_bytes > self._cfg.max_size_mb * 1_048_576:
                if required_v_kbps >= self._cfg.min_video_bitrate_kbps:
                    recommendation = f"\n💡 *Recommendation*: Compress to ~{required_v_kbps} kbps (fits in 1 file)."
                else:
                    recommendation = f"\n💡 *Recommendation*: Quality floor reached. Compress to {self._cfg.min_video_bitrate_kbps} kbps + Split."

        u_id = str(uuid.uuid4())
        URL_CACHE[u_id] = {"url": url, "time": time.monotonic()}

        keyboard = [
            [
                InlineKeyboardButton("⬇️ Download", callback_data=f"dl:{u_id}"),
                InlineKeyboardButton("🎵 Audio", callback_data=f"au:{u_id}"),
            ],
            [
                InlineKeyboardButton("📦 Compress", callback_data=f"cp:{u_id}"),
                InlineKeyboardButton("✂️ Split", callback_data=f"sp:{u_id}"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cn:{u_id}")]
        ]

        report = (
            f"📺 *{markdown_escape(title)}*\n"
            f"⏳ **Duration**: {duration_str}\n"
            f"📦 **Size**: {size_str}\n"
            f"{recommendation}\n\n"
            f"How would you like to proceed?"
        )

        await status_msg.edit_text(
            report,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
