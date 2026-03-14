from __future__ import annotations

import logging

import httpx

from alerts.base import Alert
from notifications.base import Notifier

logger = logging.getLogger(__name__)

PRIORITY_EMOJI = {
    "high": "\U0001f6a8",  # rotating light
    "medium": "\u26a0\ufe0f",  # warning
    "low": "\u2139\ufe0f",  # info
}

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier(Notifier):
    """Sends alerts to a Telegram chat via Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=10.0)

    async def send(self, alert: Alert) -> None:
        emoji = PRIORITY_EMOJI.get(alert.priority, "")
        text = f"{emoji} *{alert.headline}*\n{alert.detail}"

        try:
            resp = await self._client.post(
                API_URL.format(token=self.bot_token),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            resp.raise_for_status()
        except Exception:
            logger.warning("Telegram notification failed", exc_info=True)
