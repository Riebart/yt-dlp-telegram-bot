#!/usr/bin/env python3
"""
Telegram yt-dlp Bot
-------------------
Architecture:
  Config                 — all env-var configuration in one place
  OllamaClassifier       — LLM-based intent routing
  FFmpegProcessor        — ffprobe/ffmpeg operations (reusable for any intent)
  YtdlpDownloader        — yt-dlp download with progress logging
  BaseIntentHandler      — abstract base for intent handlers
  DownloadIntentHandler  — handles the "download" intent end-to-end
  BotRouter              — auth, classification, and dispatch
  main()                 — wires everything together and starts the bot

Adding a new intent:
  1. Add it to OllamaClassifier.SYSTEM_PROMPT
  2. Create a subclass of BaseIntentHandler
  3. Register it in main() handlers dict
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import ollama
import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest

load_dotenv()

# Global cache for URLs associated with interactive buttons
# Maps UUID -> {url: str, time: float}
URL_CACHE: dict[str, dict] = {}

# Tracking active subprocesses for the Kill Switch
# Maps chat_id -> set of subprocess.Popen objects
ACTIVE_PROCESSES: dict[int, set[subprocess.Popen]] = {}

# Set of chat_ids that have requested a cancellation
CANCELLATIONS: set[int] = set()

def track_process(chat_id: int, proc: subprocess.Popen):
    if chat_id not in ACTIVE_PROCESSES:
        ACTIVE_PROCESSES[chat_id] = set()
    ACTIVE_PROCESSES[chat_id].add(proc)

def untrack_process(chat_id: int, proc: subprocess.Popen):
    if chat_id in ACTIVE_PROCESSES:
        ACTIVE_PROCESSES[chat_id].discard(proc)
        if not ACTIVE_PROCESSES[chat_id]:
            del ACTIVE_PROCESSES[chat_id]

async def cleanup_cache_task():
    """Background task to remove old URL_CACHE entries."""
    while True:
        try:
            now = time.monotonic()
            to_del = [k for k, v in URL_CACHE.items() if now - v["time"] > 3600]
            for k in to_del:
                del URL_CACHE[k]
            if to_del:
                log.debug("Cleaned up %d expired cache entries", len(to_del))
        except Exception as e:
            log.error("Error in cache cleanup: %s", e)
        await asyncio.sleep(600)

async def _unix_to_tcp_relay(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    sock_path = os.environ.get("OLLAMA_UNIX_SOCKET", "")
    try:
        ur, uw = await asyncio.open_unix_connection(sock_path)
    except Exception as exc:
        writer.close()
        return
    async def pipe(r, w):
        try:
            while chunk := await r.read(65536):
                w.write(chunk)
                await w.drain()
        finally:
            w.close()
    await asyncio.gather(pipe(reader, uw), pipe(ur, writer))

async def start_ollama_relay(host="127.0.0.1", port=11434) -> None:
    sock_path = os.environ.get("OLLAMA_UNIX_SOCKET", "")
    if not sock_path:
        return
    server = await asyncio.start_server(_unix_to_tcp_relay, host, port)
    log.info("Ollama relay: %s:%d → %s", host, port, sock_path)
    asyncio.ensure_future(server.serve_forever())

# ---------------------------------------------------------------------------
# Logging — set up before anything else so Config can log warnings
# ---------------------------------------------------------------------------

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


log = _setup_logging(os.environ.get("LOG_LEVEL", "INFO").upper())

URL_RE = re.compile(r"https?://[^\s<>\"'{}|\\^`\[\]]+")

def markdown_escape(text: str) -> str:
    """Escape markdown special characters to prevent 'Can't parse entities' errors."""
    # Characters that need escaping in Markdown: _ * [ ] ( ) ~ ` > # + - = | { } . !
    # We focus on the ones most likely to appear in filenames: _ * [ ] `
    chars = r"_*[]`"
    return re.sub(f"([{re.escape(chars)}])", r"\\\1", text)


# ===========================================================================
# Config
# ===========================================================================

def terminate_process_group(pid: int) -> None:
    """
    Terminates a process and all its children across different platforms.
    """
    try:
        if sys.platform == "win32":
            # On Windows, use taskkill to terminate the process tree
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            # On Unix, kill the process group
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception as exc:
        log.error("Failed to terminate process group %d: %s", pid, exc)

async def check_for_cancellation(chat_id: int, status_msg: Any) -> bool:
    """
    Check if a task has been cancelled. If so, update status message and return True.
    """
    if chat_id in CANCELLATIONS:
        log.info("Cancellation detected for chat %d. Aborting operation.", chat_id)
        try:
            await status_msg.edit_text("❌ Task cancelled.")
        except Exception as e:
            log.error("Failed to edit status message during cancellation: %s", e)
        return True
    return False

def get_primary_file(directory: Path, extension: str | None = None) -> Path | None:
    """
    Find the primary output file in a directory.
    Defaults to the largest file, or the largest file with the given extension.
    """
    try:
        files = [f for f in directory.iterdir() if f.is_file()]
        if not files:
            return None
        if extension:
            files = [f for f in files if f.suffix.lower() == extension.lower()]
            if not files:
                return None
        return max(files, key=lambda f: f.stat().st_size)
    except Exception as exc:
        log.error("Error finding primary file in %s: %s", directory, exc)
        return None

class Config:
    """All configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self.bot_token:
            log.critical("TELEGRAM_BOT_TOKEN is not set — cannot start.")
            sys.exit(1)

        _raw = os.environ.get("ALLOWED_CHAT_IDS", "")
        self.allowed_chat_ids: set[int] = (
            {int(i.strip()) for i in _raw.split(",") if i.strip()}
            if _raw.strip() else set()
        )

        self.max_size_mb:      int   = int(os.environ.get("MAX_SIZE_MB",      "50"))
        self.compress_mb:      int   = int(os.environ.get("COMPRESS_MB",      "45"))
        self.audio_bps:        int   = int(os.environ.get("AUDIO_BPS",        "96000"))
        self.video_bitrate_kbps: int = int(os.environ.get("VIDEO_BITRATE_KBPS", "2000"))
        self.min_video_bitrate_kbps: int = int(os.environ.get("MIN_VIDEO_BITRATE_KBPS", "800"))
        self.preflight_duration_min: int = int(os.environ.get("PREFLIGHT_DURATION_MIN", "7"))
        self.download_timeout: int   = int(os.environ.get("DOWNLOAD_TIMEOUT", "300"))
        self.ffmpeg_location:  str   = os.environ.get("FFMPEG_LOCATION", "")
        self.save_dir:         str   = os.environ.get("SAVE_DIR", "")
        self.ollama_model:     str   = os.environ.get("OLLAMA_MODEL",  "qwen3.5:0.8b")
        self.ollama_tcp_host:  str   = os.environ.get("OLLAMA_LISTEN_TCP_HOST", "127.0.0.1")
        self.ollama_tcp_port:  str   = os.environ.get("OLLAMA_LISTEN_TCP_PORT", "11434")
        self.ollama_host:      str   = f"http://{self.ollama_tcp_host}:{self.ollama_tcp_port}"
        self.ollama_timeout:   int   = int(os.environ.get("OLLAMA_TIMEOUT", "30"))
        self.log_level:        str   = os.environ.get("LOG_LEVEL", "INFO").upper()

    def log_startup(self) -> None:
        log.info("=" * 60)
        log.info("yt-dlp Telegram bot starting")
        log.info("  MAX_SIZE_MB      = %d MiB",  self.max_size_mb)
        log.info("  COMPRESS_MB      = %d MiB",  self.compress_mb)
        log.info("  AUDIO_BPS        = %d bps",  self.audio_bps)
        log.info("  VIDEO_BITRATE_KBPS = %d kbps", self.video_bitrate_kbps)
        log.info("  MIN_VIDEO_BITRATE_KBPS = %d kbps", self.min_video_bitrate_kbps)
        log.info("  PREFLIGHT_DURATION_MIN = %d min", self.preflight_duration_min)
        log.info("  DOWNLOAD_TIMEOUT = %ds",     self.download_timeout)
        log.info("  LOG_LEVEL        = %s",       self.log_level)
        log.info("  FFMPEG_LOCATION  = %s",       self.ffmpeg_location or "(system PATH)")
        log.info("  SAVE_DIR         = %s",       self.save_dir or "(disabled)")
        log.info("  OLLAMA_MODEL     = %s",       self.ollama_model)
        log.info("  OLLAMA_HOST      = %s",       self.ollama_host)
        log.info("  OLLAMA_TIMEOUT   = %ds",      self.ollama_timeout)
        if self.allowed_chat_ids:
            log.info("  ALLOWED_CHAT_IDS = %s", self.allowed_chat_ids)
        else:
            log.warning("  ALLOWED_CHAT_IDS not set — ANY user can trigger downloads!")
        log.info("=" * 60)


# ===========================================================================
# OllamaClassifier
# ===========================================================================

class OllamaClassifier:
    """
    Classifies incoming messages into named intents using a local Ollama model.

    To add a new intent, append it to SYSTEM_PROMPT and register a handler
    in main(). No other changes needed here.
    """

    SYSTEM_PROMPT = """\
/no_think

You are the intent classifier for a Telegram bot.

Identify the user's intent and respond ONLY with a single JSON object.
No prose, no markdown, no code fences.

The default intent if it is unclear, or if only a URL or link is provided
must always be `download`.

Supported intents and their required response shapes:

  {"intent": "download"}
      The message contains a URL pointing to a video on a platform yt-dlp
      supports (YouTube, Reddit, Twitter/X, TikTok, Instagram, Vimeo,
      Twitch, Dailymotion, redgifs, etc.) with no audio-only preference.

  {"intent": "audio"}
      The message contains a URL AND the user explicitly wants audio only —
      e.g. "audio only", "just the audio", "as MP3", "extract audio",
      "download the song", or the URL points to a music/podcast platform
      (SoundCloud, Bandcamp, Mixcloud, etc.)

  {"intent": "large_video_split"}
      The URL points to a very large video (> MAX_SIZE_MB) and the user
      wants to split it into smaller chunks without re-encoding.
      Example: "this file is too big, can you split it?"

  {"intent": "large_video_compress"}
      The URL points to a large video and the user wants it compressed
      to fit within size limits, possibly with quality reduction.
      Example: "compress this video" or "make this file smaller"

  {"intent": "large_video_auto"}
      The URL points to a large video and user wants the bot to decide
      whether to split or compress automatically.
      Example: "this file is too big, do what you think is best"

  {"intent": "report_size"}
      The user wants to know the file size, duration, or details of the
      video BEFORE downloading it. This will unambiguously be identified
      if the user uses the word "stat", or "info", or "probe". But may also
      be indicated if the user uses
      Example: "how big is this?", "size?", "info", "duration", "stat", "info".

  {"intent": "unknown", "reply": "<one or two friendly sentences>"}
      Anything else: plain text, questions, non-media URLs (news articles,
      GitHub repos, Google Docs, product pages), or no URL at all.

Auto-detection rules for large videos:
- For videos > 60 minutes: recommend split (preserves quality)
- For videos < 10 minutes: recommend compress (faster, good enough)
- For videos 10-60 minutes: offer both options

As new intents are added they will be listed here.
"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._log = logging.getLogger("classifier")

    def classify_sync(self, text: str) -> tuple[str, str]:
        """
        Synchronous — call via run_in_executor.
        Returns (intent, reply).  reply is only set for 'unknown'.
        """
        client = ollama.Client(host=self._cfg.ollama_host)
        response = client.chat(
            model=self._cfg.ollama_model,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user",   "content": text},
            ],
            options={
                "temperature": 0
            },
            think=False,
            keep_alive=0
        )
        raw = response["message"]["content"].strip()
        self._log.debug("Raw response: %r", raw)

        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._log.warning("Non-JSON response (%r) — defaulting to 'download'.", raw[:200])
            return "download", ""

        intent = data.get("intent", "unknown")
        reply  = data.get("reply", "Sorry, I can't handle that message.")
        return intent, reply

    async def classify(self, text: str, timeout: int) -> tuple[str, str]:
        """Async wrapper with timeout. Fails open to 'download' on any error."""
        try:
            return await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, self.classify_sync, text),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._log.warning("Timed out after %ds — defaulting to 'download'.", timeout)
            return "download", ""
        except Exception as exc:
            self._log.warning("Error (%s) — defaulting to 'download'.", exc)
            return "download", ""


# ===========================================================================
# FFmpegProcessor
# ===========================================================================

class FFmpegProcessor:
    """
    Wraps ffprobe and ffmpeg operations.
    Reusable by any intent handler that needs video processing.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._log = logging.getLogger("ffmpeg")

    def binary(self, name: str) -> str:
        if self._cfg.ffmpeg_location:
            return str(Path(self._cfg.ffmpeg_location) / name)
        return name

    # ------------------------------------------------------------------
    # ffprobe
    # ------------------------------------------------------------------

    def get_duration(self, path: Path) -> float | None:
        """Return video duration in seconds, or None on failure."""
        try:
            result = subprocess.run(
                [
                    self.binary("ffprobe"), "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                self._log.error("ffprobe failed: %s", result.stderr.strip())
                return None
            return float(result.stdout.strip())
        except Exception as exc:
            self._log.error("ffprobe exception: %s", exc)
            return None

    def get_video_bitrate(self, path: Path) -> int | None:
        """
        Return average video bitrate in bps, or None on failure.
        Checks stream bitrate first, falls back to format bitrate.
        """
        try:
            # 1. Try stream bitrate
            result = subprocess.run(
                [
                    self.binary("ffprobe"), "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=bits_per_second",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30,
            )
            bitrate_str = result.stdout.strip() if result.returncode == 0 else ""
            
            # 2. Fallback to format bitrate if stream bitrate is missing
            if not bitrate_str:
                result = subprocess.run(
                    [
                        self.binary("ffprobe"), "-v", "error",
                        "-show_entries", "format=bit_rate",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        str(path),
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                bitrate_str = result.stdout.strip() if result.returncode == 0 else ""

            if bitrate_str:
                # Handle both integer and float bitrates (comma as decimal in some locales)
                return int(float(bitrate_str.replace(',', '.')))
            return None
        except Exception as exc:
            self._log.error("ffprobe exception: %s", exc)
            return None

    # ------------------------------------------------------------------
    # ffmpeg — size-targeted re-encode
    # ------------------------------------------------------------------

    def _check_qsv_available(self) -> bool:
        """Check for existence of Intel QSV device."""
        if sys.platform == "linux" and os.path.exists("/dev/dri/renderD128"):
            return True
        return False

    def compress_to_size(
        self,
        input_path: Path,
        target_video_bps: int,
        audio_bps: int = 0,
        output_path: Path | None = None,
        progress_callback: callable = None,
        chat_id: int | None = None,
        depth: int = 0,
    ) -> tuple[bool, Path | None, str]:
        """
        Re-encode input_path using a target video bitrate.
        """
        if depth > 1:
            return False, None, "Maximum compression retry depth exceeded."

        duration = self.get_duration(input_path)
        if duration is None or duration <= 0:
            return False, None, "Could not determine video duration."

        if target_video_bps <= 50_000:
            return False, None, f"Target bitrate too low ({target_video_bps/1000:.0f} kbps)."

        out = output_path or (input_path.parent / f"{input_path.stem}_compressed.mp4")

        # Select encoder based on hardware availability
        use_qsv = self._check_qsv_available()
        if use_qsv:
            self._log.info("Intel QSV hardware acceleration detected and enabled.")
        else:
            self._log.warning("Intel QSV not available (missing /dev/dri/renderD128). Falling back to libx264.")

        cmd = [
            self.binary("ffmpeg"), "-y",
            "-i", str(input_path),
            "-c:v", "h264_qsv" if use_qsv else "libx264",
            "-preset", "veryfast",
            "-b:v", str(target_video_bps),
            "-maxrate", str(int(target_video_bps * 1.5)),
            "-bufsize", str(target_video_bps * 2),
            "-pix_fmt", "nv12" if use_qsv else "yuv420p",
            "-vf", "scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))'",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-stats_period", "1",
            "-nostats",
        ]

        if audio_bps > 0:
            cmd.extend(["-c:a", "aac", "-b:a", f"{audio_bps//1000}k"])
        else:
            cmd.extend(["-c:a", "copy"])

        cmd.append(str(out))
        self._log.debug("ffmpeg cmd: %s", " ".join(cmd))

        t0 = time.monotonic()
        last_callback_time = 0.0
        try:
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(cmd, **popen_kwargs)
            if chat_id:
                track_process(chat_id, proc)
        except Exception as exc:
            return False, None, f"Failed to launch ffmpeg: {exc}"

        try:
            progress: dict[str, str] = {}
            if proc.stdout:
                for line in proc.stdout:
                    # Check for cancellation during processing
                    if chat_id and chat_id in CANCELLATIONS:
                        self._log.info("[%s] Cancellation detected during compression for chat %d. Terminating process %d.",
                                       input_path.name, chat_id, proc.pid)
                        terminate_process_group(proc.pid)
                        # Ensure proc.wait() won't block indefinitely if termination is slow
                        try:
                            proc.wait(timeout=5)
                            self._log.info("[%s] Process %d terminated successfully after cancellation.", input_path.name, proc.pid)
                        except subprocess.TimeoutExpired:
                            self._log.warning("[%s] Process %d did not terminate gracefully after 5s.", input_path.name, proc.pid)
                        return False, None, "Process was cancelled by user."

                    line = line.strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        progress[k.strip()] = v.strip()
                    if line.startswith("progress="):
                        try:
                            out_us = int(progress.get("out_time_us", 0))
                            pct = min(out_us / (duration * 1_000_000) * 100, 100.0)
                            size_mib = int(progress.get("total_size", 0)) / 1_048_576
                            speed = progress.get("speed", "?")
                            self._log.info("  %5.1f%%  written=%.2f MiB  speed=%s",
                                           pct, size_mib, speed)

                            # Trigger callback every 5 seconds
                            now = time.monotonic()
                            if progress_callback and (now - last_callback_time >= 5.0):
                                progress_callback(pct, size_mib, speed)
                                last_callback_time = now
                        except: pass
                        progress = {}

            proc.wait(timeout=1200)
        finally:
            if chat_id:
                untrack_process(chat_id, proc)

        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            # Handle cancellation
            if proc.returncode == -signal.SIGTERM or (sys.platform == "win32" and proc.returncode == 1):
                 return False, None, "Process was cancelled by user."

            # Fallback for audio: if "copy" failed, try re-encoding audio to AAC
            if "-c:a copy" in " ".join(cmd):
                self._log.warning("ffmpeg copy-audio failed, retrying with aac re-encode...")
                return self.compress_to_size(input_path, target_video_bps, audio_bps=64000,
                                           output_path=output_path, progress_callback=progress_callback,
                                           chat_id=chat_id, depth=depth+1)

            tail = proc.stderr.read()[-500:] if proc.stderr else "Unknown error"
            return False, None, f"ffmpeg exited {proc.returncode}: {tail}"

        return True, out, ""


# ===========================================================================
# Large Video Splitter
# ===========================================================================

class LargeVideoSplitter:
    """
    Splits a large video into smaller chunks that fit within size limits.
    Preserves original quality (lossless splitting at keyframes).
    """

    def __init__(self, cfg: Config, ffmpeg: FFmpegProcessor) -> None:
        self._cfg = cfg
        self._ffmpeg = ffmpeg
        self._log = logging.getLogger("splitter")
        self._manifest_path = None
        self._chunks = []

    def get_keyframes(self, path: Path) -> list[float]:
        """Return a list of keyframe timestamps using ffprobe."""
        try:
            cmd = [
                self._ffmpeg.binary("ffprobe"), "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "frame=pts_time",
                "-skip_frame", "nokey",
                "-of", "csv=p=0",
                str(path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                self._log.warning("ffprobe keyframe check failed (likely no keyframes or format error): %s", result.stderr.strip())
                return []

            keyframes = []
            for line in result.stdout.splitlines():
                # Cleanup: remove potential trailing commas or spaces
                cleaned = line.strip().rstrip(',')
                if not cleaned: continue
                try:
                    keyframes.append(float(cleaned))
                except ValueError:
                    self._log.debug("Skipping unparseable keyframe time: %r", cleaned)
                    continue
            return sorted(keyframes)
        except Exception as exc:
            self._log.error("Error getting keyframes: %s", exc)
            return []

    def split_video(
        self,
        input_path: Path,
        max_size_mb: float,
        chat_id: int | None = None,
    ) -> tuple[list[Path], str]:
        """
        Split input_video into chunks that fit within max_size_mb.
        Uses keyframe-accurate splitting.
        """
        input_size = input_path.stat().st_size
        max_size_bytes = int(max_size_mb * 1_048_576)

        if input_size <= max_size_bytes:
            return [input_path], ""

        duration = self._ffmpeg.get_duration(input_path)
        if duration is None or duration <= 0:
            return [], "Could not determine video duration."

        keyframes = self.get_keyframes(input_path)
        if not keyframes:
            self._log.warning("No keyframes found, falling back to time-based split.")
            # We'll use a simple fallback if keyframes can't be probed
            keyframes = [i * 30.0 for i in range(int(duration / 30.0) + 1)]

        # Group keyframes into chunks based on estimated size
        bitrate = (input_size * 8) / duration
        target_chunk_duration = (max_size_bytes * 0.9) * 8 / bitrate

        splits = [0.0]
        last_split = 0.0
        for k in keyframes:
            if k - last_split >= target_chunk_duration:
                splits.append(k)
                last_split = k
        if splits[-1] < duration - 1.0:
            splits.append(duration)

        self._log.info("Splitting into %d chunks using keyframes", len(splits) - 1)

        chunks = []
        for i in range(len(splits) - 1):
            start = splits[i]
            end = splits[i+1]
            dur = end - start
            if dur < 0.1: continue

            out_path = input_path.parent / f"{input_path.stem}_part{i+1:03d}.mp4"

            # Build ffmpeg split command
            # Using -ss BEFORE -i for fast seeking, and -t for duration.
            # -c copy for lossless.
            cmd = [
                self._ffmpeg.binary("ffmpeg"), "-y",
                "-ss", str(round(start, 3)),
                "-t", str(round(dur, 3)),
                "-i", str(input_path),
                "-c", "copy",
                "-map", "0",
                "-copyts", # Try to preserve timestamps for better player compatibility
                "-avoid_negative_ts", "make_zero",
                str(out_path),
            ]

            try:
                popen_kwargs = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["preexec_fn"] = os.setsid

                proc = subprocess.Popen(cmd, **popen_kwargs)
                if chat_id: track_process(chat_id, proc)

                # Monitor for cancellation while waiting for the process
                while proc.poll() is None:
                    if chat_id and chat_id in CANCELLATIONS:
                        self._log.info("[%s] Cancellation detected during splitting for chat %d. Terminating process %d.",
                                       input_path.name, chat_id, proc.pid)
                        terminate_process_group(proc.pid)
                        # Ensure proc.wait() won't block indefinitely if termination is slow
                        try:
                            proc.wait(timeout=5)
                            self._log.info("[%s] Process %d terminated successfully after cancellation.", input_path.name, proc.pid)
                        except subprocess.TimeoutExpired:
                            self._log.warning("[%s] Process %d did not terminate gracefully after 5s.", input_path.name, proc.pid)
                        if chat_id: untrack_process(chat_id, proc)
                        return [], "Process was cancelled by user."
                    time.sleep(0.5) # Check every 500ms

                if chat_id: untrack_process(chat_id, proc)

                if proc.returncode == 0 and out_path.exists():
                    chunks.append(out_path)
                elif proc.returncode != 0:
                    # Handle cancellation
                    if proc.returncode == -signal.SIGTERM or (sys.platform == "win32" and proc.returncode == 1):
                         return [], "Process was cancelled by user."
                    return [], f"FFmpeg failed at part {i+1}"
            except Exception as e:
                return [], str(e)

        # Generate manifest
        manifest_path = input_path.parent / f"{input_path.stem}_split_manifest.json"
        self._generate_manifest(manifest_path, chunks, input_path)
        self._chunks = chunks
        return chunks, ""

    def _generate_manifest(self, manifest_path: Path, chunks: list[Path], original: Path) -> None:
        """Generate JSON manifest for split video chunks."""
        total_size = sum(c.stat().st_size for c in chunks)
        manifest = {
            "original_file": str(original),
            "original_size_bytes": original.stat().st_size,
            "total_size_bytes": total_size,
            "total_duration_seconds": self._ffmpeg.get_duration(original) or 0,
            "split_into": len(chunks),
            "chunks": [
                {
                    "file": str(chunk.name),
                    "size_bytes": chunk.stat().st_size,
                    "size_mb": chunk.stat().st_size / 1_048_576,
                }
                for chunk in sorted(chunks, key=lambda p: p.name)
            ],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        # Generate simple playlist text file for ffmpeg concat
        playlist_path = manifest_path.with_suffix(".txt")
        with open(playlist_path, "w") as f:
            for chunk in sorted(chunks, key=lambda p: p.name):
                f.write(f"file '{chunk.name}'\n")


# ===========================================================================
# YtdlpDownloader
# ===========================================================================

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
        self._log = logging.getLogger("yt-dlp")

    def _progress_hook(self, loop, ctx=None):
        last = {"t": 0.0}
        log_ = self._log

        # Extract chat_id and msg_id from the passed context
        # The caller passes a dict with {chat_id, message_id, bot}
        # yt-dlp may also pass its own context dict or None
        if ctx:
            if isinstance(ctx, dict):
                chat_id = ctx.get("chat_id")
                msg_id = ctx.get("message_id")
                bot = ctx.get("bot")
                # yt-dlp may add chat_id/message_id to its own context dict;
                # we still use the caller-provided values for editing the status message
                if not bot and "bot" in ctx:
                    bot = ctx["bot"]
            else:
                chat_id = msg_id = bot = None
        else:
            chat_id = msg_id = bot = None

        def hook(d: dict) -> None:
            # Check for cancellation
            if chat_id and chat_id in CANCELLATIONS:
                raise Exception("Download cancelled by user")

            status = d.get("status")
            if status == "downloading":
                now = time.monotonic()
                # Update every 5 seconds
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
                    # Dispatch Telegram update to event loop
                    if bot is not None and chat_id is not None and msg_id is not None:
                        def update_status():
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


# ===========================================================================
# Intent handlers
# ===========================================================================

class BaseIntentHandler(abc.ABC):
    """All intent handlers implement this interface."""

    async def run_preflight_check(self, url: str, status_msg: Any, downloader: Any, report_handler: Any = None, duration_threshold_min: int | None = None) -> bool:
        """
        Perform metadata pre-flight check. 
        Returns True if it's okay to proceed, False if it pivoted to a report.
        """
        success, err, info = await asyncio.get_event_loop().run_in_executor(
            None, downloader.get_info_sync, url
        )

        if success:
            duration = info.get("duration", 0) or 0
            size_bytes = info.get("filesize") or info.get("filesize_approx") or 0

            cfg = getattr(self, "_cfg", None)
            if not cfg:
                return True

            # Use provided threshold or default from config
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
            # 1. Chat action
            action = ChatAction.UPLOAD_VIDEO if media_type == "video" else ChatAction.UPLOAD_VOICE
            await context.bot.send_chat_action(chat_id=chat_id, action=action)

            # 2. Open and upload
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
        self._log        = logging.getLogger("handler.download")
        self._report_handler = report_handler

    async def handle(self, message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        urls = URL_RE.findall(text)
        if not urls:
            self._log.warning("Intent=download but no URL in message.")
            await message.reply_text("⚠️ I couldn't find a URL in your message.")
            return

        url        = urls[0]
        message_id = message.message_id
        chat_id    = message.chat_id

        # --- Pre-Flight Check ---
        status_msg = await message.reply_text(f"🔍 Pre-flight check: `{url}`...", parse_mode="Markdown")

        # If it's a direct message (not a button callback), we run pre-flight.
        # Note: in handle_callback, the message passed is the bot's own report message.
        is_from_report = hasattr(message, "edit_text") and not hasattr(message, "reply_to_message") # Rough check

        if not is_from_report:
            if not await self.run_preflight_check(url, status_msg, self._downloader, self._report_handler):
                return

        # CANCELLATION CHECK: Before starting download
        if await check_for_cancellation(chat_id, status_msg):
            return

        await status_msg.edit_text(f"⬇️ Downloading…\n`{url}`\n(Send /cancel to stop)", parse_mode="Markdown")

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_tpl = os.path.join(tmpdir, "%(title).100s.%(ext)s")
                t0 = time.monotonic()

                self._log.info("Starting yt-dlp  url=%s  tmpdir=%s", url, tmpdir)
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

                prog_ctx = {"chat_id": chat_id, "message_id": status_msg.message_id, "bot": context.bot}

                # --- Download ---
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

                # CANCELLATION CHECK: After download, before processing
                if await check_for_cancellation(chat_id, status_msg):
                    return

                elapsed_dl = time.monotonic() - t0
                if not success:
                    self._log.error("yt-dlp failed  url=%s  reason=%s", url, err_msg)
                    await status_msg.edit_text(
                        f"❌ Download failed:\n```\n{err_msg.strip()[-600:]}\n```",
                        parse_mode="Markdown",
                    )
                    return

                # --- Locate file ---
                video_path = get_primary_file(Path(tmpdir))
                if not video_path:
                    self._log.error("yt-dlp exited OK but no file found in %s", tmpdir)
                    await status_msg.edit_text("❌ yt-dlp finished but no file was found.")
                    return

                size_bytes = video_path.stat().st_size
                size_mib   = size_bytes / 1_048_576
                self._log.info("Download complete  file=%s  size=%.2f MiB", video_path.name, size_mib)

                # --- Optional local archive ---
                if self._cfg.save_dir:
                    dest = Path(self._cfg.save_dir) / video_path.name
                    Path(self._cfg.save_dir).mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy(video_path, dest)
                        self._log.info("Saved local copy  path=%s", dest)
                    except Exception as exc:
                        self._log.warning("Could not save local copy  err=%s", exc)

                # --- Processing Logic ---
                upload_list = [video_path]
                was_processed = False

                if size_bytes > self._cfg.max_size_mb * 1_048_576:
                    duration = self._ffmpeg.get_duration(video_path) or 0
                    if duration <= 0:
                        self._log.warning("Could not determine duration, falling back to split.")
                        action = "split"
                    else:
                        # 1. Calculate required bitrate to hit COMPRESS_MB
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

                    # State Machine Execution
                    if "compress" in action:
                        # CANCELLATION CHECK: Before starting compression
                        if await check_for_cancellation(chat_id, status_msg):
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
                            action = "split" # Fallback

                    if action == "split":
                        # CANCELLATION CHECK: Before starting split
                        if await check_for_cancellation(chat_id, status_msg):
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

                # --- Upload Loop ---
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
class ReportSizeIntentHandler(BaseIntentHandler):
    """
    Handles the 'report_size' intent:
      1. Extract URL
      2. Fetch metadata via yt-dlp (simulate)
      3. Show info (size, duration)
      4. Offer interactive buttons for next steps
    """

    def __init__(self, cfg: Config, downloader: YtdlpDownloader) -> None:
        self._cfg        = cfg
        self._downloader = downloader
        self._log        = logging.getLogger("handler.report")

    async def handle(self, message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        urls = URL_RE.findall(text)
        if not urls:
            await message.reply_text("⚠️ No URL found.")
            return

        url = urls[0]
        status_msg = await message.reply_text(f"🔍 Fetching info for `{url}`...", parse_mode="Markdown")
        await self.send_report(status_msg, url)

    async def send_report(self, status_msg, url: str) -> None:
        """Shared logic to fetch info and show the interactive report."""
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

        # Quality recommendation
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

        # Cache URL for buttons
        u_id = str(uuid.uuid4())
        URL_CACHE[u_id] = {"url": url, "time": time.monotonic()}

        # Buttons
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
        self._log        = logging.getLogger("handler.audio")
        self._report_handler = report_handler

    async def handle(self, message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        urls = URL_RE.findall(text)
        if not urls:
            self._log.warning("Intent=audio but no URL in message.")
            await message.reply_text("⚠️ I couldn't find a URL in your message.")
            return

        url        = urls[0]
        message_id = message.message_id
        chat_id    = message.chat_id

        # --- Pre-Flight Check ---
        status_msg = await message.reply_text(f"🔍 Pre-flight check (audio): `{url}`...", parse_mode="Markdown")

        is_from_report = hasattr(message, "edit_text") and not hasattr(message, "reply_to_message")

        if not is_from_report:
            if not await self.run_preflight_check(url, status_msg, self._downloader, self._report_handler, duration_threshold_min=30):
                return

        await status_msg.edit_text(f"⬇️ Downloading audio…\n`{url}`", parse_mode="Markdown")
        with tempfile.TemporaryDirectory() as tmpdir:
            # yt-dlp replaces the extension after ffmpeg postprocessing,
            # so use a fixed stem and locate the .mp3 by extension after download
            output_tpl = os.path.join(tmpdir, "%(title).100s.%(ext)s")
            t0 = time.monotonic()

            self._log.info("Starting yt-dlp audio  url=%s  tmpdir=%s", url, tmpdir)
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

            # Prepare context for yt-dlp progress hook
            # yt-dlp's progress hook expects a context dict with chat_id/message_id
            prog_ctx = {"chat_id": chat_id, "message_id": status_msg.message_id}
            prog_ctx["bot"] = context.bot

            # --- Download ---
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

            # --- Locate the MP3 ---
            audio_path = get_primary_file(Path(tmpdir), extension=".mp3")
            if not audio_path:
                # Fallback: just grab the largest file if ffmpeg used a different ext
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

            # --- Size check (no compression path for audio) ---
            if size_bytes > self._cfg.max_size_mb * 1_048_576:
                await status_msg.edit_text(
                    f"❌ Audio file is {size_mib:.1f} MiB — over the "
                    f"{self._cfg.max_size_mb} MiB Telegram limit."
                )
                return

            # --- Optional local archive ---
            if self._cfg.save_dir:
                dest = Path(self._cfg.save_dir) / audio_path.name
                Path(self._cfg.save_dir).mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy(audio_path, dest)
                    self._log.info("Saved local copy  path=%s", dest)
                except Exception as exc:
                    self._log.warning("Could not save local copy  dest=%s  err=%s", dest, exc)

            # --- Upload ---
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


# ===========================================================================
# BotRouter
# ===========================================================================

class BotRouter:
    """
    Receives every Telegram message, runs auth, classifies intent via Ollama,
    and dispatches to the appropriate handler.
    """

    def __init__(
        self,
        cfg: Config,
        classifier: OllamaClassifier,
        handlers: dict[str, BaseIntentHandler],
    ) -> None:
        self._cfg        = cfg
        self._classifier = classifier
        self._handlers   = handlers
        self._log        = logging.getLogger("router")

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        if message is None:
            return

        chat_id  = message.chat_id
        msg_id   = message.message_id
        username = (message.from_user.username
                    or message.from_user.full_name
                    or "unknown")
        text     = (message.text or message.caption or "").strip()

        self._log.info(
            "Inbound  chat_id=%s  msg_id=%s  user=%s  text=%r",
            chat_id, msg_id, username, text[:120],
        )

        # Clear any stale cancellation flags for this chat before processing a new request
        CANCELLATIONS.discard(chat_id)

        # Auth
        if self._cfg.allowed_chat_ids and chat_id not in self._cfg.allowed_chat_ids:
            self._log.warning("Unauthorised  chat_id=%s  user=%s", chat_id, username)
            await message.reply_text("⛔ You are not authorised to use this bot.")
            return

        if not text:
            return

        # Classify
        status_msg = await message.reply_text(
            f"🧠 Classifying…  (`{self._cfg.ollama_model}`)",
            parse_mode="Markdown",
        )

        self._log.info("Classifying  model=%s", self._cfg.ollama_model)
        intent, decline_reply = await self._classifier.classify(
            text, self._cfg.ollama_timeout
        )
        self._log.info("Intent=%r", intent)

        await status_msg.edit_text(
            f"🧠 `{self._cfg.ollama_model}` → `{intent}`",
            parse_mode="Markdown",
        )
        await asyncio.sleep(2)
        await status_msg.delete()

        # Dispatch
        handler = self._handlers.get(intent)
        if handler is not None:
            await handler.handle(message, context, text)
        else:
            self._log.info("No handler for intent=%r — sending decline.", intent)
            await message.reply_text(decline_reply)

    async def handle_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Terminate all active processes for this chat."""
        message = update.message
        if message is None: return
        chat_id = message.chat_id

        self._log.info("Cancellation request received for chat %d", chat_id)

        # Flag for yt-dlp to stop
        CANCELLATIONS.add(chat_id)

        # Kill any active subprocesses (ffmpeg/ffprobe)
        procs = ACTIVE_PROCESSES.get(chat_id, set())
        count = len(procs)
        for proc in list(procs):
            try:
                self._log.debug("Attempting to kill process %d for chat %d", proc.pid, chat_id)
                terminate_process_group(proc.pid)
                self._log.info("Successfully sent kill signal to process %d", proc.pid)
            except Exception as e:
                self._log.error("Error killing process %d for chat %d: %s", proc.pid, chat_id, e)

        if count > 0 or chat_id in CANCELLATIONS:
            await message.reply_text(f"🛑 Cancelled {count} active task(s).")
        else:
            await message.reply_text("ℹ️ No active tasks to cancel.")

    async def handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query:
            return

        await query.answer()
        chat_id = query.message.chat_id
        data = query.data
        if not data or ":" not in data:
            return

        action, u_id = data.split(":", 1)
        cached = URL_CACHE.get(u_id)
        if not cached:
            await query.edit_message_text("⚠️ This session has expired or the URL is no longer in cache.")
            return

        url = cached["url"]
        intent_map = {
            "dl": "download",
            "au": "audio",
            "cp": "large_video_compress",
            "sp": "large_video_split",
        }

        if action == "cn":
            await query.edit_message_text("❌ Action cancelled.")
            URL_CACHE.pop(u_id, None)
            return

        intent = intent_map.get(action)
        handler = self._handlers.get(intent)

        if handler:
            URL_CACHE.pop(u_id, None)
            # Flag that we are starting a fresh task
            CANCELLATIONS.discard(chat_id)
            await query.edit_message_text(f"✅ Selected: `{intent}`\nProcessing: `{url}`", parse_mode="Markdown")
            await handler.handle(query.message, context, url)
        else:
            await query.edit_message_text(f"❌ No handler for action: {action}")


# ===========================================================================
# Error handler
# ===========================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception in update handler:", exc_info=context.error)


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    cfg = Config()
    cfg.log_startup()

    # Start the Unix domain socket proxy.
    # Doesn't work though, so the entrypoint handles it with socat
    # asyncio.get_event_loop().run_until_complete(start_ollama_relay())

    ffmpeg     = FFmpegProcessor(cfg)
    downloader = YtdlpDownloader(cfg)
    classifier = OllamaClassifier(cfg)

    # Register intent handlers here.
    # Adding a new intent = new handler class + one line below.
    # Large video handling intents all go through DownloadIntentHandler

    # We create the reporter first so we can pass it to the download handler for pre-flight pivots
    reporter = ReportSizeIntentHandler(cfg, downloader)

    handlers: dict[str, BaseIntentHandler] = {
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

    # Register the kill switch command
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
