import logging
import os
import subprocess
import sys
import signal
import time
import json
from pathlib import Path
from bot.config import Config
from bot.utils import terminate_process_group, track_process, untrack_process

log = logging.getLogger("bot.processor")

class FFmpegProcessor:
    """
    Wraps ffprobe and ffmpeg operations.
    Reusable by any intent handler that needs video processing.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._log = log

    def binary(self, name: str) -> str:
        if self._cfg.ffmpeg_location:
            return str(Path(self._cfg.ffmpeg_location) / name)
        return name

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
                return int(float(bitrate_str.replace(',', '.')))
            return None
        except Exception as exc:
            self._log.error("ffprobe exception: %s", exc)
            return None

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
                    from bot import state
                    if chat_id and chat_id in state.CANCELLATIONS:
                        self._log.info("[%s] Cancellation detected during compression for chat %d. Terminating process %d.",
                                       input_path.name, chat_id, proc.pid)
                        terminate_process_group(proc.pid)
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
            if proc.returncode == -signal.SIGTERM or (sys.platform == "win32" and proc.returncode == 1):
                 return False, None, "Process was cancelled by user."

            if "-c:a copy" in " ".join(cmd):
                self._log.warning("ffmpeg copy-audio failed, retrying with aac re-encode...")
                return self.compress_to_size(input_path, target_video_bps, audio_bps=64000,
                                           output_path=output_path, progress_callback=progress_callback,
                                           chat_id=chat_id, depth=depth+1)

            tail = proc.stderr.read()[-500:] if proc.stderr else "Unknown error"
            return False, None, f"ffmpeg exited {proc.returncode}: {tail}"

        return True, out, ""

class LargeVideoSplitter:
    """
    Splits a large video into smaller chunks that fit within size limits.
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
                self._log.warning("ffprobe keyframe check failed: %s", result.stderr.strip())
                return []

            keyframes = []
            for line in result.stdout.splitlines():
                cleaned = line.strip().rstrip(',')
                if not cleaned: continue
                try:
                    keyframes.append(float(cleaned))
                except ValueError:
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
            keyframes = [i * 30.0 for i in range(int(duration / 30.0) + 1)]

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

            cmd = [
                self._ffmpeg.binary("ffmpeg"), "-y",
                "-ss", str(round(start, 3)),
                "-t", str(round(dur, 3)),
                "-i", str(input_path),
                "-c", "copy",
                "-map", "0",
                "-copyts",
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

                while proc.poll() is None:
                    from bot import state
                    if chat_id and chat_id in state.CANCELLATIONS:
                        self._log.info("[%s] Cancellation detected during splitting for chat %d. Terminating process %d.",
                                       input_path.name, chat_id, proc.pid)
                        terminate_process_group(proc.pid)
                        try:
                            proc.wait(timeout=5)
                            self._log.info("[%s] Process %d terminated successfully after cancellation.", input_path.name, proc.pid)
                        except subprocess.TimeoutExpired:
                            self._log.warning("[%s] Process %d did not terminate gracefully after 5s.", input_path.name, proc.pid)
                        if chat_id: untrack_process(chat_id, proc)
                        return [], "Process was cancelled by user."
                    time.sleep(0.5)

                if chat_id: untrack_process(chat_id, proc)

                if proc.returncode == 0 and out_path.exists():
                    chunks.append(out_path)
                elif proc.returncode != 0:
                    if proc.returncode == -signal.SIGTERM or (sys.platform == "win32" and proc.returncode == 1):
                         return [], "Process was cancelled by user."
                    return [], f"FFmpeg failed at part {i+1}"
            except Exception as e:
                return [], str(e)

        manifest_path = input_path.parent / f"{input_path.stem}_split_manifest.json"
        self._generate_manifest(manifest_path, chunks, input_path)
        self._chunks = chunks
        return chunks, ""

    def _generate_manifest(self, manifest_path: Path, chunks: list[Path], original: Path) -> None:
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

        playlist_path = manifest_path.with_suffix(".txt")
        with open(playlist_path, "w") as f:
            for chunk in sorted(chunks, key=lambda p: p.name):
                f.write(f"file '{chunk.name}'\n")
