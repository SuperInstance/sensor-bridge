"""Tests for the MQTT client module."""
import json
import time
import pytest
from sensor_bridge.normalizer import Normalizer
from sensor_bridge.pattern_detector import PatternDetector
from sensor_bridge.escalation import EscalationRouter
from sensor_bridge.history import SensorHistory, HistoryConfig
from sensor_bridge.mqtt_client import SensorBridgeMQTTClient


@pytest.fixture
def device_configs():
    return {
        "engine_1": {
            "sensors": {
                "rpm": {"type": "float", "unit": "rpm", "min": 0, "max": 4000,
                         "thresholds": {"warning": 3400, "critical": 3600},
                         "anomaly": {"max_rate_of_change": 500, "stuck_band": 5, "stuck_readings": 10}},
                "coolant_temp": {"type": "float", "unit": "°C", "min": 0, "max": 150,
                                  "thresholds": {"warning": 90, "critical": 100},
                                  "anomaly": {"max_rate_of_change": 10, "stuck_band": 0.5, "stuck_readings": 10}},
            },
        },
    }


@pytest.fixture
def components(device_configs, tmp_path):
    normalizer = Normalizer(device_configs)
    pattern_detector = PatternDetector(device_configs=device_configs, rolling_window=20)
    history = SensorHistory(HistoryConfig(db_path=str(tmp_path / "test.db")))
    escalation = EscalationRouter(
        escalation_config={
            "level_0_normal": {"notify": [], "log": True, "page_laforge": False},
            "level_1_warning": {"notify": [], "log": True, "page_laforge": False, "laforge_review_on_next": True},
            "level_2_alert": {"notify": ["captain"], "log": True, "page_laforge": True, "laforge_priority": "normal"},
            "level_3_critical": {"notify": ["captain", "crew"], "log": True, "page_laforge": True, "laforge_priority": "urgent"},
        },
        log_dir=str(tmp_path / "escalation"),
    )
    return normalizer, pattern_detector, history, escalation


@pytest.fixture
def client(components):
    normalizer, pattern_detector, history, escalation = components
    return SensorBridgeMQTTClient(
        normalizer=normalizer,
        pattern_detector=pattern_detector,
        history=history,
        escalation_router=escalation,
    )


class TestProcessMessage:
    def test_single_sensor_message(self, client):
        client.process_message("vessel/engine_1/sensors/rpm", 1500.0)
        assert client.messages_received >= 1
        assert client.readings_processed == 1

    def test_batch_status_message(self, client):
        payload = {"rpm": 1500, "coolant_temp": 85}
        client.process_message("vessel/engine_1/status", payload)
        assert client.readings_processed == 2

    def test_json_string_payload(self, client):
        client.process_message(
            "vessel/engine_1/sensors/rpm",
            json.dumps({"value": 1500}),
        )
        assert client.readings_processed == 1

    def test_raw_string_payload(self, client):
        client.process_message("vessel/engine_1/sensors/rpm", "1500.0")
        assert client.readings_processed == 1

    def test_callback_on_readings(self, client):
        received = []
        client.on_readings = lambda readings: received.extend(readings)
        client.process_message("vessel/engine_1/sensors/rpm", 1500.0)
        assert len(received) == 1
        assert received[0].value == 1500.0

    def test_callback_on_patterns(self, client):
        received = []
        client.on_patterns = lambda patterns: received.extend(patterns)
        # Send a reading that crosses a threshold
        client.process_message("vessel/engine_1/sensors/coolant_temp", 95.0)
        assert len(received) >= 1

    def test_callback_on_escalation(self, client):
        received = []
        client.on_escalation = lambda action: received.append(action)
        client.process_message("vessel/engine_1/sensors/coolant_temp", 105.0)
        assert len(received) >= 1

    def test_unknown_device_ignored(self, client):
        client.process_message("vessel/unknown/sensors/rpm", 1500.0)
        assert client.readings_processed == 0

    def test_stats_increment(self, client):
        initial = client.messages_received
        client.process_message("vessel/engine_1/sensors/rpm", 1500.0)
        client.process_message("vessel/engine_1/sensors/rpm", 2000.0)
        assert client.messages_received == initial + 2
        assert client.readings_processed == 2


class TestStatus:
    def test_get_status(self, client):
        status = client.get_status()
        assert "connected" in status
        assert "broker" in status
        assert "messages_received" in status
        assert "readings_processed" in status
