import abc
import logging
from pathlib import Path
from typing import Any
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

log = logging.getLogger("bot.handlers.base")

class BaseIntentHandler(abc.ABC):
    """All intent handlers implement this interface."""

    async def run_preflight_check(self, url: str, status_msg: Any, downloader: Any, report_handler: Any = None, duration_threshold_min: int | None = None) -> bool:
        """
        Perform metadata pre-flight check. 
        Returns True if it's okay to proceed, False if it pivoted to a report.
        """
        import asyncio
        success, err, info = await asyncio.get_event_loop().run_in_executor(
            None, downloader.get_info_sync, url
        )

        if success:
            duration = info.get("duration", 0) or 0
            size_bytes = info.get("filesize") or info.get("filesize_approx") or 0

            cfg = getattr(self, "_cfg", None)
            if not cfg:
                return True

            thresh_min = duration_threshold_min if duration_threshold_min is not None else cfg.preflight_duration_min
            
            large_size = size_bytes > cfg.max_size_mb * 1_048_576
            long_duration = duration > thresh_min * 60

            if large_size or long_duration:
                if report_handler:
                    await report_handler.send_report(status_msg, url)
                    return False
                else:
                    self._log.warning("No report_handler registered, proceeding anyway.")

        return True

    async def upload_media(self, message, context: ContextTypes.DEFAULT_TYPE, file_path: Path, caption: str, media_type: str) -> None:
        """
        Centralized media upload: handles chat action, file opening, and the actual upload.
        media_type: 'video' or 'audio'
        """
        chat_id = message.chat_id
        try:
            action = ChatAction.UPLOAD_VIDEO if media_type == "video" else ChatAction.UPLOAD_VOICE
            await context.bot.send_chat_action(chat_id=chat_id, action=action)

            with open(file_path, "rb") as fh:
                if media_type == "video":
                    await message.reply_video(
                        video=fh, supports_streaming=True,
                        caption=caption, parse_mode="Markdown",
                        write_timeout=180,
                    )
                else:
                    await message.reply_audio(
                        audio=fh,
                        caption=caption, parse_mode="Markdown",
                        write_timeout=180,
                    )
        except Exception as exc:
            self._log.error("Upload failed for %s: %s", file_path.name, exc)
            await message.reply_text(f"❌ Failed to upload {media_type}: {exc}")

    @abc.abstractmethod
    async def handle(
        self,
        message,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None: ...
