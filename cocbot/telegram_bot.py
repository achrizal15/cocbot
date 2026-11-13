"""Telegram Bot for COCBOT notifications and remote commands."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

import httpx

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}"


class TelegramBot:
    """Lightweight Telegram bot using httpx — no external telegram lib."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._base = _API.format(token=token)
        self._offset: int = 0
        self._running = False
        self._thread: threading.Thread | None = None

    # ── send helpers ─────────────────────────────────────────────

    def send(self, text: str) -> None:
        """Send a text message. Silently logs on failure."""
        try:
            with httpx.Client(timeout=10) as c:
                r = c.post(
                    f"{self._base}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                )
                if not r.is_success:
                    log.warning("Telegram sendMessage failed: %s", r.text)
        except Exception:
            log.exception("Telegram sendMessage error")

    def send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        """Send a photo (PNG/JPEG bytes)."""
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(
                    f"{self._base}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption},
                    files={"photo": ("screenshot.png", photo_bytes, "image/png")},
                )
                if not r.is_success:
                    log.warning("Telegram sendPhoto failed: %s", r.text)
        except Exception:
            log.exception("Telegram sendPhoto error")

    # ── polling ──────────────────────────────────────────────────

    def start_polling(self, handler: Callable[[str], str]) -> None:
        """Start long-polling for commands in a background daemon thread.

        *handler* receives the command text (e.g. ``"/status"``) and returns
        a response string to send back.
        """
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll, args=(handler,), daemon=True, name="tg-poll"
        )
        self._thread.start()

    def stop_polling(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _poll(self, handler: Callable[[str], str]) -> None:
        while self._running:
            try:
                with httpx.Client(timeout=35) as c:
                    r = c.get(
                        f"{self._base}/getUpdates",
                        params={
                            "offset": self._offset,
                            "timeout": 30,
                            "allowed_updates": '["message"]',
                        },
                    )
                if not r.is_success:
                    log.warning("Telegram getUpdates failed: %s", r.text)
                    continue
                data = r.json()
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    if not text.startswith("/"):
                        continue
                    # Only respond to the configured chat
                    if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                        continue
                    try:
                        reply = handler(text)
                    except Exception:
                        log.exception("Telegram handler error for %r", text)
                        reply = "⚠️ Internal error"
                    self.send(reply)
            except Exception:
                log.exception("Telegram poll error")
                # brief backoff before retry
                import time
                time.sleep(5)
