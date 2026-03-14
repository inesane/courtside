from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """Load configuration from YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path) as f:
        return yaml.safe_load(f)


def get_alert_config(config: dict[str, Any], rule_name: str) -> dict[str, Any]:
    """Get config for a specific alert rule, with defaults."""
    alerts = config.get("alerts", {})
    return alerts.get(rule_name, {"enabled": True})
