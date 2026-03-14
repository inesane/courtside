from __future__ import annotations

import logging

import httpx

from alerts.base import Alert
from notifications.base import Notifier

logger = logging.getLogger(__name__)

PRIORITY_EMOJI = {
    "high": ":rotating_light:",
    "medium": ":warning:",
    "low": ":information_source:",
}


class DiscordNotifier(Notifier):
    """Sends alerts to a Discord channel via webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=10.0)

    async def send(self, alert: Alert) -> None:
        emoji = PRIORITY_EMOJI.get(alert.priority, "")
        content = f"{emoji} **{alert.headline}**\n{alert.detail}"

        try:
            resp = await self._client.post(
                self.webhook_url,
                json={"content": content},
            )
            resp.raise_for_status()
        except Exception:
            logger.warning("Discord notification failed", exc_info=True)
