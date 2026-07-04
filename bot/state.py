"""Global state for the bot to avoid circular imports."""
from typing import Any
import asyncio
import time
import logging

log = logging.getLogger("bot.state")

# Map of URL -> metadata/timestamp for interactive reports
# Format: { u_id: {"url": str, "time": float} }
URL_CACHE: dict[str, dict[str, Any]] = {}

# Map of job_id -> set of active subprocesses
# Format: { job_id: {Process, ...} }
ACTIVE_PROCESSES: dict[str, set] = {}

# Set of job_ids currently requesting cancellation
CANCELLATIONS: set[str] = set()

# Map of chat_id -> set of active job_ids
# Format: { chat_id: {job_id, ...} }
USER_JOBS: dict[int, set[str]] = {}

async def cleanup_cache_task():
    """Periodically remove expired entries from URL_CACHE."""
    while True:
        try:
            now = time.monotonic()
            # Expire entries older than 1 hour
            expired = [u_id for u_id, data in URL_CACHE.items() 
                       if now - data.get("time", 0) > 3600]
            for u_id in expired:
                del URL_CACHE[u_id]
            
            if expired:
                log.debug("Cleaned up %d expired cache entries.", len(expired))
            
            await asyncio.sleep(600) # Check every 10 mins
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("Error in cache cleanup task: %s", e)
            await asyncio.sleep(60)
