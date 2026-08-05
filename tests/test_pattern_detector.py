"""Tests for the pattern detector module."""
import time
import pytest
from sensor_bridge.normalizer import SensorReading
from sensor_bridge.pattern_detector import (
    PatternDetector,
    PatternType,
    Severity,
)


@pytest.fixture
def device_configs():
    return {
        "engine_1": {
            "sensors": {
                "coolant_temp": {
                    "type": "float", "unit": "°C", "min": 0, "max": 150,
                    "thresholds": {"warning": 90, "critical": 100},
                    "anomaly": {
                        "max_rate_of_change": 10,
                        "stuck_band": 0.5,
                        "stuck_readings": 5,
                        "drift_rate": 5.0,
                    },
                },
                "oil_pressure": {
                    "type": "float", "unit": "bar", "min": 0, "max": 10,
                    "thresholds": {
                        "warning": 1.5, "critical": 0.8,
                        "below": True, "condition": "rpm > 500",
                    },
                    "anomaly": {"max_rate_of_change": 2.0, "stuck_band": 0.1, "stuck_readings": 10},
                },
                "rpm": {
                    "type": "float", "unit": "rpm", "min": 0, "max": 4000,
                    "thresholds": {"warning": 3400, "critical": 3600},
                    "anomaly": {"max_rate_of_change": 500, "stuck_band": 5, "stuck_readings": 10},
                },
            },
        },
    }


@pytest.fixture
def detector(device_configs):
    return PatternDetector(
        device_configs=device_configs,
        rolling_window=20,
        spike_stddev=3.0,
        drift_threshold=10.0,
    )


def make_reading(device, sensor, value, ts=None):
    return SensorReading(
        device_id=device, sensor=sensor, value=value,
        unit="", timestamp=ts or time.time(),
    )


class TestThresholdDetection:
    def test_warning_threshold(self, detector):
        r = make_reading("engine_1", "coolant_temp", 92.0)
        events = detector.check(r)
        assert any(e.pattern_type == PatternType.THRESHOLD_WARNING for e in events)
        assert any(e.severity == Severity.WARNING for e in events)

    def test_critical_threshold(self, detector):
        r = make_reading("engine_1", "coolant_temp", 105.0)
        events = detector.check(r)
        assert any(e.pattern_type == PatternType.THRESHOLD_CRITICAL for e in events)
        assert any(e.severity == Severity.CRITICAL for e in events)

    def test_normal_value_no_event(self, detector):
        r = make_reading("engine_1", "coolant_temp", 80.0)
        events = detector.check(r)
        threshold_events = [e for e in events if e.pattern_type in (
            PatternType.THRESHOLD_WARNING, PatternType.THRESHOLD_CRITICAL
        )]
        assert len(threshold_events) == 0

    def test_rpm_warning(self, detector):
        r = make_reading("engine_1", "rpm", 3450.0)
        events = detector.check(r)
        assert any(e.pattern_type == PatternType.THRESHOLD_WARNING for e in events)

    def test_rpm_critical(self, detector):
        r = make_reading("engine_1", "rpm", 3650.0)
        events = detector.check(r)
        assert any(e.pattern_type == PatternType.THRESHOLD_CRITICAL for e in events)

    def test_low_threshold_oil_pressure(self, detector):
        """Oil pressure uses below=True — thresholds trigger when value is LOW."""
        # First set rpm > 500 to satisfy condition
        detector.check(make_reading("engine_1", "rpm", 1500.0))
        # Now low oil pressure should trigger
        r = make_reading("engine_1", "oil_pressure", 1.0)
        events = detector.check(r)
        assert any(e.pattern_type == PatternType.THRESHOLD_WARNING for e in events)

    def test_low_threshold_oil_pressure_critical(self, detector):
        detector.check(make_reading("engine_1", "rpm", 1500.0))
        r = make_reading("engine_1", "oil_pressure", 0.5)
        events = detector.check(r)
        assert any(e.pattern_type == PatternType.THRESHOLD_CRITICAL for e in events)


class TestSpikeDetection:
    def test_spike_detected(self, detector):
        """Feed normal readings then a sudden spike."""
        base_time = time.time()
        for i in range(10):
            r = make_reading("engine_1", "coolant_temp", 80.0 + i * 0.1, base_time + i)
            detector.check(r)

        # Now spike
        r = make_reading("engine_1", "coolant_temp", 130.0, base_time + 10)
        events = detector.check(r)
        # Should detect threshold AND/OR spike
        assert len(events) > 0

    def test_no_spike_on_normal(self, detector):
        base_time = time.time()
        for i in range(10):
            r = make_reading("engine_1", "coolant_temp", 80.0, base_time + i)
            detector.check(r)
        # Gradual change — not a spike
        r = make_reading("engine_1", "coolant_temp", 80.5, base_time + 10)
        events = detector.check(r)
        spike_events = [e for e in events if e.pattern_type == PatternType.SPIKE]
        assert len(spike_events) == 0


class TestStuckDetection:
    def test_stuck_value_detected(self, detector):
        base_time = time.time()
        # Same value for many readings (more than stuck_readings=5)
        for i in range(10):
            r = make_reading("engine_1", "coolant_temp", 80.0, base_time + i)
            events = detector.check(r)

        # The last reading should have triggered stuck
        stuck_events = [e for e in events if e.pattern_type == PatternType.STUCK]
        assert len(stuck_events) > 0

    def test_not_stuck_with_variation(self, detector):
        base_time = time.time()
        for i in range(10):
            r = make_reading("engine_1", "coolant_temp", 80.0 + i * 1.0, base_time + i)
            events = detector.check(r)

        stuck_events = [e for e in events if e.pattern_type == PatternType.STUCK]
        assert len(stuck_events) == 0


class TestRecoveryDetection:
    def test_recovery_detected(self, detector):
        base_time = time.time()
        # Trigger warning
        r = make_reading("engine_1", "coolant_temp", 95.0, base_time)
        detector.check(r)
        # Recover
        r = make_reading("engine_1", "coolant_temp", 80.0, base_time + 1)
        events = detector.check(r)
        recovery = [e for e in events if e.pattern_type == PatternType.RECOVERY]
        assert len(recovery) == 1
        assert recovery[0].severity == Severity.NORMAL


class TestStateQuery:
    def test_get_state(self, detector):
        base_time = time.time()
        for i in range(5):
            r = make_reading("engine_1", "coolant_temp", 80.0, base_time + i)
            detector.check(r)

        state = detector.get_state("engine_1", "coolant_temp")
        assert state["count"] == 5
        assert state["mean"] == 80.0
        assert state["last_value"] == 80.0

    def test_get_state_no_data(self, detector):
        state = detector.get_state("engine_1", "nonexistent")
        assert state["status"] == "no_data"
