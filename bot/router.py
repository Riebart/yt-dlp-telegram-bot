import asyncio
import logging
from typing import Any
from telegram import Update
from telegram.ext import ContextTypes
from bot.config import Config
from bot.classifier import OllamaClassifier
from bot.handlers.base import BaseIntentHandler
from bot.state import URL_CACHE, CANCELLATIONS, ACTIVE_PROCESSES
from bot.utils import terminate_process_group

log = logging.getLogger("bot.router")

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
        self._log        = log

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
        CANCELLATIONS.clear()

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
        # Ensure it's fully awaited before moving on
        await asyncio.sleep(0)

        self._log.info("Classifying  model=%s", self._cfg.ollama_model)
        try:
            intent, decline_reply = await self._classifier.classify(
                text, self._cfg.ollama_timeout
            )
        except Exception as e:
            self._log.exception("Classifier crash: %s", e)
            await status_msg.edit_text("❌ Sorry, I encountered an error while classifying your request.")
            return

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
