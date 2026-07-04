import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bot.config import Config
from bot.downloader import YtdlpDownloader
from bot.handlers.base import BaseIntentHandler
from bot.handlers.report import ReportSizeIntentHandler
from bot.utils import (
    markdown_escape,
    get_primary_file,
    check_for_cancellation,
)
from bot.state import CANCELLATIONS

log = logging.getLogger("bot.handlers.audio")

class AudioIntentHandler(BaseIntentHandler):
    """
    Handles the 'audio' intent:
      1. Extract URL from text
      2. Download best audio via yt-dlp, postprocess to MP3
      3. Upload to Telegram as an audio message
    """

    def __init__(self, cfg: Config, downloader: YtdlpDownloader, report_handler: ReportSizeIntentHandler = None) -> None:
        self._cfg        = cfg
        self._downloader = downloader
        self._log        = log
        self._report_handler = report_handler

    async def handle(self, message, context: ContextTypes.DEFAULT_TYPE, text: str, override_action: str | None = None) -> None:
        import re
        URL_RE = re.compile(r'''https?://[^\s<>"'{}|\^`\[\]]+''')
        urls = URL_RE.findall(text)
        if not urls:
            self._log.warning("Intent=audio but no URL in message.")
            await message.reply_text("⚠️ I couldn't find a URL in your message.")
            return

        url        = urls[0]
        message_id = message.message_id
        chat_id    = message.chat_id

        status_msg = await message.reply_text(f"🔍 Pre-flight check (audio): `{url}`...", parse_mode="Markdown")

        if override_action is None:
            if not await self.run_preflight_check(url, status_msg, self._downloader, self._report_handler, duration_threshold_min=30):
                return

        await status_msg.edit_text(f"⬇️ Downloading audio…\n`{url}`", parse_mode="Markdown")
        with tempfile.TemporaryDirectory() as tmpdir:
            output_tpl = os.path.join(tmpdir, "%(title).100s.%(ext)s")
            t0 = time.monotonic()

            self._log.info("Starting yt-dlp audio  url=%s  tmpdir=%s", url, tmpdir)
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

            prog_ctx = {"chat_id": chat_id, "message_id": status_msg.message_id, "bot": context.bot}

            try:
                loop = asyncio.get_running_loop()
                success, err_msg, _ = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, self._downloader.download_audio_sync, url, output_tpl, prog_ctx, loop
                    ),
                    timeout=self._cfg.download_timeout,
                )
            except asyncio.TimeoutError:
                self._log.error("Audio download timed out after %ds", self._cfg.download_timeout)
                await status_msg.edit_text(f"❌ Timed out after {self._cfg.download_timeout}s.")
                return
            except Exception as exc:
                self._log.exception("Unexpected audio download error  url=%s", url)
                await status_msg.edit_text(f"❌ Unexpected error: {exc}")
                return

            elapsed_dl = time.monotonic() - t0

            if not success:
                self._log.error("yt-dlp audio failed  url=%s  reason=%s", url, err_msg)
                await status_msg.edit_text(
                    f"❌ Download failed:\n```\n{err_msg.strip()[-600:]}\n```",
                    parse_mode="Markdown",
                )
                return

            audio_path = get_primary_file(Path(tmpdir), extension=".mp3")
            if not audio_path:
                audio_path = get_primary_file(Path(tmpdir))
                if not audio_path:
                    await status_msg.edit_text("❌ yt-dlp finished but no audio file was found.")
                    return

            size_bytes = audio_path.stat().st_size
            size_mib   = size_bytes / 1_048_576
            self._log.info(
                "Audio download complete  file=%s  size=%.2f MiB  elapsed=%.1fs",
                audio_path.name, size_mib, elapsed_dl,
            )

            if size_bytes > self._cfg.max_size_mb * 1_048_576:
                await status_msg.edit_text(
                    f"❌ Audio file is {size_mib:.1f} MiB — over the "
                    f"{self._cfg.max_size_mb} MiB Telegram limit."
                )
                return

            # Optional local archive
            if self._cfg.save_dir:
                dest = Path(self._cfg.save_dir) / audio_path.name
                Path(self._cfg.save_dir).mkdir(parents=True, exist_ok=True)
                try:
                    import shutil
                    shutil.copy(audio_path, dest)
                    self._log.info("Saved local copy  path=%s", dest)
                except Exception as exc:
                    self._log.warning("Could not save local copy  dest=%s  err=%s", dest, exc)

            self._log.info("Uploading audio  file=%s  size=%.2f MiB", audio_path.name, size_mib)
            await status_msg.edit_text(f"📤 Uploading {size_mib:.1f} MiB…")
            
            escaped_title = markdown_escape(audio_path.stem)
            try:
                await self.upload_media(message, context, audio_path, f"✅ {escaped_title}", "audio")
                self._log.info("Audio upload request completed for %s", audio_path.name)
            except Exception as exc:
                self._log.error("Audio upload failed  file=%s  err=%s", audio_path.name, exc, exc_info=True)
                await status_msg.edit_text(f"❌ Upload failed: {exc}")
            finally:
                await status_msg.delete()
