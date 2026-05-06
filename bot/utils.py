import logging
from pathlib import Path
import os
import sys
import subprocess
import signal
import asyncio
from typing import Any

from bot.state import ACTIVE_PROCESSES

log = logging.getLogger("bot.utils")

def markdown_escape(text: str) -> str:
    """Escape characters that have special meaning in Telegram MarkdownV2."""
    # Note: This is a simplified escape list.
    for char in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\{char}")
    return text

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

async def check_for_cancellation(chat_id: int, status_msg: Any, cancellations_set: set) -> bool:
    """
    Check if a task has been cancelled. If so, update status message and return True.
    """
    if chat_id in cancellations_set:
        log.info("Cancellation detected for chat %d. Aborting operation.", chat_id)
        try:
            await status_msg.edit_text("❌ Task cancelled.")
        except Exception as e:
            log.error("Failed to edit status message during cancellation: %s", e)
        return True
    return False

def track_process(chat_id: int, proc, active_processes: Any = None):
    """Add a process to the active processes tracking map."""
    if active_processes is None:
        active_processes = ACTIVE_PROCESSES
    if chat_id not in active_processes:
        active_processes[chat_id] = set()
    active_processes[chat_id].add(proc)

def untrack_process(chat_id: int, proc, active_processes: Any = None):
    """Remove a process from the active processes tracking map."""
    if active_processes is None:
        active_processes = ACTIVE_PROCESSES
    if chat_id in active_processes:
        active_processes[chat_id].discard(proc)
        if not active_processes[chat_id]:
            del active_processes[chat_id]

async def error_handler(update: Any, context: Any) -> None:
    """Global error handler for the Telegram bot."""
    log.error("Unhandled exception in update handler:", exc_info=context.error)

async def start_ollama_relay():
    """Start the Ollama Unix-to-TCP relay if configured."""
    socket_path = os.environ.get("OLLAMA_UNIX_SOCKET")
    if not socket_path:
        return None
    
    try:
        # This is a placeholder for the actual relay logic
        # In a real implementation, this would start a background task
        log.info("Starting Ollama relay for socket %s", socket_path)
        return True
    except Exception as e:
        log.error("Failed to start Ollama relay: %s", e)
        return None

async def _unix_to_tcp_relay(reader, writer):
    """Internal relay logic."""
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
