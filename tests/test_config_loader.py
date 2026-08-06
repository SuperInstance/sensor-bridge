"""
Tests for sensor_bridge config_loader — YAML config loading and validation.

Tests cover:
- Default values when keys are missing
- Custom values from YAML
- File-not-found error
- Empty config handling
- Device config parsing
- History config defaults
"""

import pytest
from pathlib import Path
from unittest.mock import mock_open, patch
import yaml

from sensor_bridge.config_loader import BridgeConfig, load_config
from sensor_bridge.history import HistoryConfig


# ─── Fixtures ─────────────────────────────────────────────

@pytest.fixture
def minimal_config_yaml():
    return """
broker:
  host: 192.168.1.100
  port: 1883
"""


@pytest.fixture
def full_config_yaml():
    return """
broker:
  host: mqtt.example.com
  port: 8883
  client_id: test-bridge
  keepalive: 120
  username: admin
  password: secret
topic_root: vessel
devices:
  engine:
    type: yanmar
    poll_interval: 1.0
history:
  db_path: /data/test.db
  retention_days: 30
  compaction_after_hours: 12
  max_high_res: 50000
pattern_detector:
  oil_pressure:
    low_warning: 10
escalation:
  cooldown_seconds: 300
exocortex:
  enabled: true
"""


@pytest.fixture
def config_file(tmp_path, full_config_yaml):
    p = tmp_path / "config.yaml"
    p.write_text(full_config_yaml)
    return p


@pytest.fixture
def minimal_config_file(tmp_path, minimal_config_yaml):
    p = tmp_path / "minimal.yaml"
    p.write_text(minimal_config_yaml)
    return p


# ─── load_config Tests ───────────────────────────────────

class TestLoadConfig:
    def test_load_full_config(self, config_file):
        cfg = load_config(config_file)
        assert cfg.broker_host == "mqtt.example.com"
        assert cfg.broker_port == 8883
        assert cfg.client_id == "test-bridge"
        assert cfg.keepalive == 120
        assert cfg.broker_username == "admin"
        assert cfg.broker_password == "secret"
        assert cfg.topic_root == "vessel"

    def test_load_minimal_config_uses_defaults(self, minimal_config_file):
        cfg = load_config(minimal_config_file)
        assert cfg.broker_host == "192.168.1.100"
        assert cfg.broker_port == 1883
        assert cfg.client_id == "sensor-bridge"
        assert cfg.keepalive == 60
        assert cfg.broker_username == ""
        assert cfg.topic_root == "vessel"

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config("/nonexistent/path/config.yaml")

    def test_empty_yaml_uses_all_defaults(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = load_config(p)
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883
        assert cfg.client_id == "sensor-bridge"

    def test_devices_parsed(self, config_file):
        cfg = load_config(config_file)
        assert "engine" in cfg.devices
        assert cfg.devices["engine"]["type"] == "yanmar"

    def test_empty_devices_default(self, minimal_config_file):
        cfg = load_config(minimal_config_file)
        assert cfg.devices == {}


# ─── History Config Tests ────────────────────────────────

class TestHistoryConfig:
    def test_custom_history_config(self, config_file):
        cfg = load_config(config_file)
        assert cfg.history_config.db_path == "/data/test.db"
        assert cfg.history_config.retention_days == 30
        assert cfg.history_config.compaction_after_hours == 12
        assert cfg.history_config.max_high_res == 50000

    def test_default_history_config(self, minimal_config_file):
        cfg = load_config(minimal_config_file)
        assert cfg.history_config.db_path == "data/sensor_history.db"
        assert cfg.history_config.retention_days == 90
        assert cfg.history_config.compaction_after_hours == 24
        assert cfg.history_config.max_high_res == 100_000


# ─── Pattern Detector & Escalation Config Tests ──────────

class TestPatternDetectorConfig:
    def test_pattern_detector_parsed(self, config_file):
        cfg = load_config(config_file)
        assert "oil_pressure" in cfg.pattern_detector
        assert cfg.pattern_detector["oil_pressure"]["low_warning"] == 10

    def test_escalation_parsed(self, config_file):
        cfg = load_config(config_file)
        assert cfg.escalation["cooldown_seconds"] == 300

    def test_exocortex_parsed(self, config_file):
        cfg = load_config(config_file)
        assert cfg.exocortex["enabled"] is True

    def test_empty_defaults(self, minimal_config_file):
        cfg = load_config(minimal_config_file)
        assert cfg.pattern_detector == {}
        assert cfg.escalation == {}
        assert cfg.exocortex == {}


# ─── BridgeConfig Dataclass Tests ────────────────────────

class TestBridgeConfig:
    def test_config_is_dataclass(self):
        import dataclasses
        assert dataclasses.is_dataclass(BridgeConfig)

    def test_config_fields(self):
        cfg = BridgeConfig(
            broker_host="localhost",
            broker_port=1883,
            client_id="test",
            keepalive=60,
            broker_username="",
            broker_password="",
            topic_root="vessel",
            devices={},
            history_config=HistoryConfig(),
            pattern_detector={},
            escalation={},
            exocortex={},
        )
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883


# ─── Edge Cases ──────────────────────────────────────────

class TestEdgeCases:
    def test_config_with_extra_keys_ignored(self, tmp_path):
        p = tmp_path / "extra.yaml"
        p.write_text("broker:\n  host: test\n  unknown_key: value\nfoo: bar\n")
        cfg = load_config(p)
        assert cfg.broker_host == "test"

    def test_config_path_as_string(self, tmp_path):
        p = tmp_path / "strpath.yaml"
        p.write_text("broker:\n  host: testhost\n")
        cfg = load_config(str(p))
        assert cfg.broker_host == "testhost"

    def test_config_path_as_pathlib(self, tmp_path):
        p = tmp_path / "pathlib.yaml"
        p.write_text("broker:\n  host: plhost\n")
        cfg = load_config(Path(p))
        assert cfg.broker_host == "plhost"
