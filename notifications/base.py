from __future__ import annotations

from abc import ABC, abstractmethod

from alerts.base import Alert


class Notifier(ABC):
    """Base class for notification channels."""

    @abstractmethod
    async def send(self, alert: Alert) -> None:
        ...
