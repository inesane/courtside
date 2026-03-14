from __future__ import annotations

from alerts.base import Alert
from notifications.base import Notifier

# ANSI colors
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
BOLD = "\033[1m"
RESET = "\033[0m"

PRIORITY_COLORS = {
    "high": RED,
    "medium": YELLOW,
    "low": GREEN,
}


class ConsoleNotifier(Notifier):
    """Prints alerts to the terminal with color formatting."""

    async def send(self, alert: Alert) -> None:
        color = PRIORITY_COLORS.get(alert.priority, "")
        priority_tag = f"[{alert.priority.upper()}]"

        print(f"\n{color}{BOLD}{'='*60}")
        print(f"  {priority_tag} {alert.headline}")
        print(f"  {alert.detail}")
        print(f"{'='*60}{RESET}\n")
