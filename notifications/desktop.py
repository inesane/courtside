from __future__ import annotations

import logging

from alerts.base import Alert
from notifications.base import Notifier

logger = logging.getLogger(__name__)


class DesktopNotifier(Notifier):
    """Sends OS-level desktop notifications using plyer."""

    async def send(self, alert: Alert) -> None:
        try:
            from plyer import notification

            notification.notify(
                title=alert.headline,
                message=alert.detail,
                app_name="Sports Alerts",
                timeout=10,
            )
        except Exception:
            logger.warning("Desktop notification failed", exc_info=True)
