import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
import shutil
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bot.config import Config
from bot.downloader import YtdlpDownloader
from bot.processor import FFmpegProcessor, LargeVideoSplitter
from bot.handlers.base import BaseIntentHandler
from bot.handlers.report import ReportSizeIntentHandler
from bot.utils import (
    markdown_escape,
    get_primary_file,
    check_for_cancellation,
    track_process,
    untrack_process,
)
from bot.state import CANCELLATIONS

log = logging.getLogger("bot.handlers.download")

class DownloadIntentHandler(BaseIntentHandler):
    """
    Handles the 'download' intent:
      1. Extract URL from text
      2. Download via yt-dlp
      3. Process (Compress/Split) if over size limit
      4. Upload (all chunks if split) to Telegram
    """

    def __init__(self, cfg: Config, downloader: YtdlpDownloader, ffmpeg: FFmpegProcessor, report_handler: ReportSizeIntentHandler = None) -> None:
        self._cfg        = cfg
        self._downloader = downloader
        self._ffmpeg     = ffmpeg
        self._splitter   = LargeVideoSplitter(cfg, ffmpeg)
        self._log        = log
        self._report_handler = report_handler

    async def handle(self, message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        import re
        URL_RE = re.compile(r'''https?://[^\s<>"'{}|\^`\[\]]+''')
        urls = URL_RE.findall(text)
        if not urls:
            self._log.warning("Intent=download but no URL in message.")
            await message.reply_text("⚠️ I couldn't find a URL in your message.")
            return

        url        = urls[0]
        message_id = message.message_id
        chat_id    = message.chat_id

        status_msg = await message.reply_text(f"🔍 Pre-flight check: `{url}`...", parse_mode="Markdown")

        is_from_report = hasattr(message, "edit_text") and not hasattr(message, "reply_to_message")

        if not is_from_report:
            if not await self.run_preflight_check(url, status_msg, self._downloader, self._report_handler):
                return

        if await check_for_cancellation(chat_id, status_msg, CANCELLATIONS):
            return

        await status_msg.edit_text(f"⬇️ Downloading…\n`{url}`\n(Send /cancel to stop)", parse_mode="Markdown")

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_tpl = os.path.join(tmpdir, "%(title).100s.%(ext)s")
                t0 = time.monotonic()

                self._log.info("Starting yt-dlp  url=%s  tmpdir=%s", url, tmpdir)
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

                prog_ctx = {"chat_id": chat_id, "message_id": status_msg.message_id, "bot": context.bot}

                try:
                    loop = asyncio.get_running_loop()
                    success, err_msg, info = await asyncio.wait_for(
                        loop.run_in_executor(
                            None, self._downloader.download_sync, url, output_tpl, prog_ctx, loop
                        ),
                        timeout=self._cfg.download_timeout,
                    )
                except asyncio.TimeoutError:
                    self._log.error("Download timed out after %ds", self._cfg.download_timeout)
                    await status_msg.edit_text(f"❌ Timed out after {self._cfg.download_timeout}s.")
                    return
                except Exception as exc:
                    if "cancelled" in str(exc).lower():
                        self._log.info("Download loop interrupted by cancellation for chat %d.", chat_id)
                        await status_msg.edit_text("❌ Download cancelled.")
                        return
                    self._log.exception("Unexpected download error  url=%s", url)
                    await status_msg.edit_text(f"❌ Unexpected error: {exc}")
                    return

                if await check_for_cancellation(chat_id, status_msg, CANCELLATIONS):
                    return

                elapsed_dl = time.monotonic() - t0
                if not success:
                    self._log.error("yt-dlp failed  url=%s  reason=%s", url, err_msg)
                    await status_msg.edit_text(
                        f"❌ Download failed:\n```\n{err_msg.strip()[-600:]}\n```",
                        parse_mode="Markdown",
                    )
                    return

                video_path = get_primary_file(Path(tmpdir))
                if not video_path:
                    self._log.error("yt-dlp exited OK but no file found in %s", tmpdir)
                    await status_msg.edit_text("❌ yt-dlp finished but no file was found.")
                    return

                size_bytes = video_path.stat().st_size
                size_mib   = size_bytes / 1_048_576
                self._log.info("Download complete  file=%s  size=%.2f MiB", video_path.name, size_mib)

                if self._cfg.save_dir:
                    dest = Path(self._cfg.save_dir) / video_path.name
                    Path(self._cfg.save_dir).mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy(video_path, dest)
                        self._log.info("Saved local copy  path=%s", dest)
                    except Exception as exc:
                        self._log.warning("Could not save local copy  err=%s", exc)

                upload_list = [video_path]
                was_processed = False

                if size_bytes > self._cfg.max_size_mb * 1_048_576:
                    duration = self._ffmpeg.get_duration(video_path) or 0
                    if duration <= 0:
                        self._log.warning("Could not determine duration, falling back to split.")
                        action = "split"
                    else:
                        target_bits = self._cfg.compress_mb * 1024 * 1024 * 8
                        audio_bits = self._cfg.audio_bps * duration
                        video_bits = target_bits - audio_bits
                        required_v_bps = int(video_bits / duration)
                        required_v_kbps = required_v_bps // 1000

                        self._log.info("Processing decision: required_v_kbps=%d, min_v_kbps=%d",
                                       required_v_kbps, self._cfg.min_video_bitrate_kbps)

                        if required_v_kbps >= self._cfg.min_video_bitrate_kbps:
                            action = "compress"
                            target_kbps = required_v_kbps
                            reason = f"Compressing to {target_kbps} kbps to fit in {self._cfg.max_size_mb}MB."
                        else:
                            action = "compress_then_split"
                            target_kbps = self._cfg.min_video_bitrate_kbps
                            reason = (f"Quality floor reached ({self._cfg.min_video_bitrate_kbps} kbps). "
                                      f"Will compress then split.")

                    await status_msg.edit_text(f"⚠️ {size_mib:.1f} MiB exceeds limit.\n{reason}\n🔄 Processing…")

                    if "compress" in action:
                        if await check_for_cancellation(chat_id, status_msg, CANCELLATIONS):
                            return

                        await status_msg.edit_text(f"🔄 Compressing to {target_kbps} kbps…")

                        loop = asyncio.get_running_loop()
                        def ffmpeg_progress(pct, size_mib, speed):
                            def update():
                                loop.create_task(
                                    status_msg.edit_text(
                                        f"🔄 Compressing: {pct:.1f}% ({size_mib:.1f} MiB)\n"
                                        f"Speed: {speed} | Target: {target_kbps} kbps\n"
                                        f"(Send /cancel to stop)"
                                    )
                                )
                            loop.call_soon_threadsafe(update)

                        ok, compressed, err = await asyncio.get_event_loop().run_in_executor(
                            None, self._ffmpeg.compress_to_size, video_path,
                            target_kbps * 1000, self._cfg.audio_bps, None, ffmpeg_progress, chat_id
                        )
                        if ok:
                            video_path = compressed
                            size_mib = video_path.stat().st_size / 1_048_576
                            was_processed = True
                            upload_list = [video_path]

                            if size_mib > self._cfg.max_size_mb:
                                action = "split"
                            else:
                                action = "done"
                        else:
                            if "cancelled" in err.lower():
                                await status_msg.edit_text("❌ Processing cancelled.")
                                return
                            self._log.error("Compression failed: %s", err)
                            action = "split"

                    if action == "split":
                        if await check_for_cancellation(chat_id, status_msg, CANCELLATIONS):
                            return

                        await status_msg.edit_text("🔄 Splitting into chunks…")
                        chunks, err = await asyncio.get_event_loop().run_in_executor(
                            None, self._splitter.split_video, video_path, float(self._cfg.max_size_mb), chat_id
                        )
                        if chunks:
                            upload_list = chunks
                            was_processed = True
                        else:
                            if "cancelled" in err.lower():
                                await status_msg.edit_text("❌ Splitting cancelled.")
                                return
                            await status_msg.edit_text(f"❌ Split failed: {err}")
                            return

                total_chunks = len(upload_list)
                for i, up_path in enumerate(upload_list):
                    if chat_id in CANCELLATIONS:
                        await status_msg.edit_text("❌ Upload cancelled.")
                        return

                    chunk_info = f" (part {i+1}/{total_chunks})" if total_chunks > 1 else ""
                    up_mib = up_path.stat().st_size / 1_048_576

                    await status_msg.edit_text(f"📤 Uploading{chunk_info} — {up_mib:.1f} MiB…")
                    
                    escaped_title = markdown_escape(video_path.stem)
                    caption = f"✅ {escaped_title}{chunk_info}"
                    if was_processed:
                        if "compressed" in up_path.name:
                            v_kbps = target_kbps if 'target_kbps' in locals() else "?"
                            caption += f"\n_(re-encoded to ~{v_kbps} kbps)_"
                        if "part" in up_path.name:
                            caption += f"\n_(lossless split part {i+1}/{total_chunks})_"

                    await self.upload_media(message, context, up_path, caption, "video")

                await status_msg.delete()
        finally:
            CANCELLATIONS.discard(chat_id)
