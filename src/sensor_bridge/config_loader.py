"""
Config loader — loads and validates the sensor bridge configuration.

Provides a typed BridgeConfig object that the bridge components use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .history import HistoryConfig

logger = logging.getLogger(__name__)


@dataclass
class BridgeConfig:
    """Top-level configuration for the sensor bridge."""
    broker_host: str
    broker_port: int
    client_id: str
    keepalive: int
    broker_username: str
    broker_password: str
    topic_root: str
    devices: dict[str, Any]
    history_config: HistoryConfig
    pattern_detector: dict[str, Any]
    escalation: dict[str, Any]
    exocortex: dict[str, Any]


def load_config(config_path: str | Path) -> BridgeConfig:
    """
    Load configuration from a YAML file.

    Args:
        config_path: Path to the config.yaml file.

    Returns:
        BridgeConfig with all settings.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    broker = raw.get("broker", {})
    history_raw = raw.get("history", {})
    pd_raw = raw.get("pattern_detector", {})

    return BridgeConfig(
        broker_host=broker.get("host", "localhost"),
        broker_port=broker.get("port", 1883),
        client_id=broker.get("client_id", "sensor-bridge"),
        keepalive=broker.get("keepalive", 60),
        broker_username=broker.get("username", ""),
        broker_password=broker.get("password", ""),
        topic_root=raw.get("topic_root", "vessel"),
        devices=raw.get("devices", {}),
        history_config=HistoryConfig(
            db_path=history_raw.get("db_path", "data/sensor_history.db"),
            retention_days=history_raw.get("retention_days", 90),
            compaction_after_hours=history_raw.get("compaction_after_hours", 24),
            max_high_res=history_raw.get("max_high_res", 100_000),
        ),
        pattern_detector=pd_raw,
        escalation=raw.get("escalation", {}),
        exocortex=raw.get("exocortex", {}),
    )
