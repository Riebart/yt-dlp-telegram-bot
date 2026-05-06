import logging
import os
import subprocess
import sys
import time
from pathlib import Path
import yt_dlp
from bot.config import Config
from bot.utils import terminate_process_group, track_process, untrack_process

log = logging.getLogger("bot.downloader")

class _YtdlpLogger:
    _log = logging.getLogger("yt-dlp")

    def debug(self, msg: str) -> None:
        if msg.startswith("[download]") or msg.startswith("[ffmpeg]"):
            self._log.info(msg)
        else:
            self._log.debug(msg)

    def info(self, msg: str) -> None:
        self._log.info(msg)

    def warning(self, msg: str) -> None:
        self._log.warning(msg)

    def error(self, msg: str) -> None:
        self._log.error(msg)

class YtdlpDownloader:
    """Downloads media via the yt-dlp Python API with 5-second progress logging."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._log = log

    def _progress_hook(self, loop, ctx=None):
        last = {"t": 0.0}
        log_ = self._log

        if ctx:
            if isinstance(ctx, dict):
                chat_id = ctx.get("chat_id")
                msg_id = ctx.get("message_id")
                bot = ctx.get("bot")
                if not bot and "bot" in ctx:
                    bot = ctx["bot"]
            else:
                chat_id = msg_id = bot = None
        else:
            chat_id = msg_id = bot = None

        def hook(d: dict) -> None:
            # We import CANCELLATIONS locally to avoid circular dependency 
            # since we are in the infrastructure layer.
            from bot import state
            if chat_id and chat_id in state.CANCELLATIONS:
                raise Exception("Download cancelled by user")

            status = d.get("status")
            if status == "downloading":
                now = time.monotonic()
                if now - last["t"] >= 5.0:
                    last["t"] = now
                    pct    = d.get("_percent_str", "?%").strip()
                    speed  = d.get("_speed_str",   "?/s").strip()
                    eta    = d.get("_eta_str",     "?").strip()
                    frag   = d.get("fragment_index")
                    nfrag  = d.get("fragment_count")
                    total  = d.get("_total_bytes_str") or d.get("_total_bytes_estimate_str", "?")
                    fi     = f"  fragment {frag}/{nfrag}" if frag and nfrag else ""
                    log_.info("Downloading  %s  speed=%s  ETA=%s  total=%s%s",
                              pct, speed, eta, total, fi)
                    if bot is not None and chat_id is not None and msg_id is not None:
                        def update_status():
                            import asyncio
                            loop.create_task(
                                bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=msg_id,
                                    text=f"⬇️ {pct}  speed={speed}  ETA={eta}  total={total}{fi}"
                                )
                            )
                        loop.call_soon_threadsafe(update_status)
            elif status == "finished":
                size    = d.get("_total_bytes_str") or f"{d.get('total_bytes', 0)/1_048_576:.2f} MiB"
                elapsed = d.get("elapsed")
                log_.info("Finished  size=%s%s  →  %s",
                          size,
                          f"  elapsed={elapsed:.1f}s" if elapsed else "",
                          Path(d["filename"]).name)
            elif status == "error":
                log_.error("Error: %s", d.get("error"))
        return hook

    def download_sync(self, url: str, output_template: str, context=None, loop=None) -> tuple[bool, str, dict]:
        """
        Synchronous — call via run_in_executor.
        Returns (success, error_message, info_dict).
        """
        opts = {
            "format": (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/best[ext=mp4]/best"
            ),
            "merge_output_format": "mp4",
            "noplaylist":          True,
            "outtmpl":             output_template,
            "logger":              _YtdlpLogger(),
            "progress_hooks":      [self._progress_hook(loop, context)],
            "noprogress":          False,
            "quiet":               False,
            **({"ffmpeg_location": self._cfg.ffmpeg_location}
               if self._cfg.ffmpeg_location else {}),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
            return True, "", info
        except yt_dlp.utils.DownloadError as exc:
            return False, str(exc), {}
        except Exception as exc:
            log.exception("Unexpected yt-dlp error for %s", url)
            return False, str(exc), {}

    def download_audio_sync(self, url: str, output_template: str, context=None, loop=None) -> tuple[bool, str, dict]:
        """
        Download audio-only and postprocess to MP3 via ffmpeg.
        Returns (success, error_message, info_dict).
        """
        opts = {
            "format":               "bestaudio/best",
            "noplaylist":           True,
            "outtmpl":              output_template,
            "logger":               _YtdlpLogger(),
            "progress_hooks":       [self._progress_hook(loop, context)],
            "noprogress":           False,
            "quiet":                False,
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "192",
            }],
            **({"ffmpeg_location": self._cfg.ffmpeg_location}
            if self._cfg.ffmpeg_location else {}),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
            return True, "", info
        except yt_dlp.utils.DownloadError as exc:
            return False, str(exc), {}
        except Exception as exc:
            log.exception("Unexpected yt-dlp error for %s", url)
            return False, str(exc), {}

    def get_info_sync(self, url: str) -> tuple[bool, str, dict]:
        """
        Fetch video metadata without downloading.
        Returns (success, error_message, info_dict).
        """
        opts = {
            "simulate":             True,
            "quiet":                True,
            "noplaylist":           True,
            **({"ffmpeg_location": self._cfg.ffmpeg_location}
               if self._cfg.ffmpeg_location else {}),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
            return True, "", info
        except Exception as exc:
            return False, str(exc), {}
