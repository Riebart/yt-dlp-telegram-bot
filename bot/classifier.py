import json
import logging
import re
import ollama
from bot.config import Config

log = logging.getLogger("bot.classifier")

class OllamaClassifier:
    """
    Classifies incoming messages into named intents using a local Ollama model.
    """

    SYSTEM_PROMPT = """
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
        self._log = log

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
        import asyncio
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
