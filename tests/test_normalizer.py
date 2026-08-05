"""Tests for the normalizer module."""
import time
import pytest
from sensor_bridge.normalizer import Normalizer, SensorReading


@pytest.fixture
def device_configs():
    return {
        "engine_ensign_1": {
            "description": "Generic Diesel Engine Monitor",
            "sensors": {
                "rpm": {"type": "float", "unit": "rpm", "min": 0, "max": 4000},
                "coolant_temp": {
                    "type": "float", "unit": "°C", "min": 0, "max": 150,
                    "thresholds": {"warning": 90, "critical": 100},
                },
                "oil_pressure": {
                    "type": "float", "unit": "bar", "min": 0, "max": 10,
                    "thresholds": {"warning": 1.5, "critical": 0.8, "below": True},
                },
            },
        },
        "weather_1": {
            "sensors": {
                "temperature": {"type": "float", "unit": "°C", "min": -40, "max": 60},
            },
        },
    }


@pytest.fixture
def normalizer(device_configs):
    return Normalizer(device_configs)


class TestSensorReading:
    def test_creation(self):
        r = SensorReading(
            device_id="dev1", sensor="temp", value=25.5,
            unit="°C", timestamp=time.time(),
        )
        assert r.device_id == "dev1"
        assert r.value == 25.5
        assert r.quality == "good"

    def test_iso_timestamp(self):
        r = SensorReading(
            device_id="dev1", sensor="temp", value=25.0,
            unit="°C", timestamp=0,
        )
        assert r.iso_timestamp == "1970-01-01T00:00:00Z"

    def test_to_dict(self):
        r = SensorReading(
            device_id="dev1", sensor="temp", value=25.0,
            unit="°C", timestamp=1000.0,
        )
        d = r.to_dict()
        assert d["device_id"] == "dev1"
        assert d["value"] == 25.0

    def test_from_dict(self):
        d = {"device_id": "d", "sensor": "s", "value": "42.5", "unit": "V"}
        r = SensorReading.from_dict(d)
        assert r.value == 42.5


class TestNormalizer:
    def test_normalize_float(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "rpm", 1500.0)
        assert r is not None
        assert r.value == 1500.0
        assert r.unit == "rpm"
        assert r.quality == "good"

    def test_normalize_string(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "rpm", "1500")
        assert r is not None
        assert r.value == 1500.0

    def test_normalize_dict(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "rpm", {"value": 1200, "unit": "rpm"})
        assert r is not None
        assert r.value == 1200.0
        assert r.unit == "rpm"

    def test_unknown_device(self, normalizer):
        r = normalizer.normalize("unknown_device", "rpm", 1500.0)
        assert r is None

    def test_unknown_sensor(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "unknown_sensor", 1500.0)
        assert r is None

    def test_quality_suspect_high(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "rpm", 5000.0)
        assert r is not None
        assert r.quality == "suspect"

    def test_quality_suspect_low(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "coolant_temp", -5.0)
        assert r is not None
        assert r.quality == "suspect"

    def test_quality_bad_nan(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "rpm", float("nan"))
        assert r is not None
        assert r.quality == "bad"

    def test_quality_bad_inf(self, normalizer):
        r = normalizer.normalize("engine_ensign_1", "rpm", float("inf"))
        assert r is not None
        assert r.quality == "bad"

    def test_normalize_topic_single(self, normalizer):
        readings = normalizer.normalize_topic(
            "vessel/engine_ensign_1/sensors/rpm", 1500.0
        )
        assert len(readings) == 1
        assert readings[0].sensor == "rpm"
        assert readings[0].value == 1500.0

    def test_normalize_topic_batch(self, normalizer):
        payload = {"rpm": 1500, "coolant_temp": 85, "oil_pressure": 3.5}
        readings = normalizer.normalize_topic(
            "vessel/engine_ensign_1/status", payload
        )
        assert len(readings) == 3
        sensors = {r.sensor for r in readings}
        assert sensors == {"rpm", "coolant_temp", "oil_pressure"}

    def test_normalize_topic_bad(self, normalizer):
        readings = normalizer.normalize_topic("bad/topic", 1500.0)
        assert len(readings) == 0

    def test_normalize_topic_skips_metadata(self, normalizer):
        payload = {"rpm": 1500, "device": "ensign", "uptime": 12345, "temperature": 85}
        readings = normalizer.normalize_topic(
            "vessel/engine_ensign_1/status", payload
        )
        # "device" and "uptime" should be skipped
        sensors = {r.sensor for r in readings}
        assert "device" not in sensors
        assert "uptime" not in sensors

    def test_known_devices(self, normalizer):
        devices = normalizer.known_devices()
        assert "engine_ensign_1" in devices
        assert "weather_1" in devices

    def test_known_sensors(self, normalizer):
        sensors = normalizer.known_sensors("engine_ensign_1")
        assert "rpm" in sensors
        assert "coolant_temp" in sensors
        assert "oil_pressure" in sensors
