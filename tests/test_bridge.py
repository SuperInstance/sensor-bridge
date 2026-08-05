"""Tests for the bridge orchestrator."""
import pytest
from sensor_bridge.bridge import Bridge
from sensor_bridge.config_loader import BridgeConfig
from sensor_bridge.history import HistoryConfig


@pytest.fixture
def config():
    return BridgeConfig(
        broker_host="localhost",
        broker_port=1883,
        client_id="test-bridge",
        keepalive=60,
        broker_username="",
        broker_password="",
        topic_root="vessel",
        devices={
            "engine_1": {
                "sensors": {
                    "coolant_temp": {
                        "type": "float", "unit": "°C", "min": 0, "max": 150,
                        "thresholds": {"warning": 90, "critical": 100},
                        "anomaly": {"max_rate_of_change": 10, "stuck_band": 0.5, "stuck_readings": 10},
                    },
                    "rpm": {
                        "type": "float", "unit": "rpm", "min": 0, "max": 4000,
                        "thresholds": {"warning": 3400, "critical": 3600},
                        "anomaly": {"max_rate_of_change": 500, "stuck_band": 5, "stuck_readings": 10},
                    },
                },
            },
        },
        history_config=HistoryConfig(db_path=":memory:"),
        pattern_detector={"rolling_window": 20, "spike_stddev": 3.0, "drift_threshold": 10.0, "stuck_check_enabled": True},
        escalation={
            "level_0_normal": {"notify": [], "log": True, "page_laforge": False},
            "level_1_warning": {"notify": [], "log": True, "page_laforge": False, "laforge_review_on_next": True},
            "level_2_alert": {"notify": ["captain"], "log": True, "page_laforge": True, "laforge_priority": "normal"},
            "level_3_critical": {"notify": ["captain", "crew"], "log": True, "page_laforge": True, "laforge_priority": "urgent"},
        },
        exocortex={},
    )


@pytest.fixture
def bridge(config, tmp_path):
    # Override db_path to temp dir
    config.history_config.db_path = str(tmp_path / "test_bridge.db")
    return Bridge(config)


class TestBridgeInit:
    def test_components_initialized(self, bridge):
        assert bridge.normalizer is not None
        assert bridge.pattern_detector is not None
        assert bridge.history is not None
        assert bridge.escalation_router is not None
        assert bridge.mqtt_client is not None

    def test_mqtt_callbacks_wired(self, bridge):
        assert bridge.mqtt_client.on_readings is not None
        assert bridge.mqtt_client.on_patterns is not None
        assert bridge.mqtt_client.on_escalation is not None


class TestInjectReading:
    def test_inject_normal(self, bridge):
        events = bridge.inject_reading("engine_1", "coolant_temp", 80.0)
        # Normal reading — no events expected
        assert len(events) == 0

    def test_inject_warning(self, bridge):
        events = bridge.inject_reading("engine_1", "coolant_temp", 92.0)
        assert len(events) >= 1
        assert any(e.pattern_type.name == "THRESHOLD_WARNING" for e in events)

    def test_inject_critical(self, bridge):
        events = bridge.inject_reading("engine_1", "coolant_temp", 105.0)
        assert len(events) >= 1
        assert any(e.pattern_type.name == "THRESHOLD_CRITICAL" for e in events)

    def test_inject_unknown_device(self, bridge):
        events = bridge.inject_reading("unknown", "coolant_temp", 80.0)
        assert len(events) == 0


class TestStatus:
    def test_get_status(self, bridge):
        status = bridge.get_status()
        assert "connected" in status
        assert "devices" in status
        assert "engine_1" in status["devices"]
        assert "escalation" in status
