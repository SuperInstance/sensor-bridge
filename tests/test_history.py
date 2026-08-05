"""Tests for the history module."""
import time
import pytest
from sensor_bridge.normalizer import SensorReading
from sensor_bridge.history import SensorHistory, HistoryConfig


@pytest.fixture
def history(tmp_path):
    config = HistoryConfig(
        db_path=str(tmp_path / "test_history.db"),
        retention_days=7,
        compaction_after_hours=1,
        max_high_res=100,
    )
    return SensorHistory(config)


def make_reading(device, sensor, value, ts=None, unit="°C"):
    return SensorReading(
        device_id=device, sensor=sensor, value=value,
        unit=unit, timestamp=ts or time.time(),
    )


class TestStoreAndQuery:
    def test_store_single(self, history):
        r = make_reading("dev1", "temp", 25.0)
        history.store(r)

        recent = history.query_recent("dev1", "temp", limit=1)
        assert len(recent) == 1
        assert recent[0]["value"] == 25.0

    def test_store_batch(self, history):
        readings = [
            make_reading("dev1", "temp", 25.0 + i, ts=time.time() + i)
            for i in range(10)
        ]
        count = history.store_batch(readings)
        assert count == 10

        recent = history.query_recent("dev1", "temp", limit=10)
        assert len(recent) == 10

    def test_store_multiple_devices(self, history):
        history.store(make_reading("dev1", "temp", 25.0))
        history.store(make_reading("dev2", "temp", 30.0))

        d1 = history.query_recent("dev1", "temp", limit=1)
        d2 = history.query_recent("dev2", "temp", limit=1)
        assert d1[0]["value"] == 25.0
        assert d2[0]["value"] == 30.0

    def test_query_recent_chronological(self, history):
        base = time.time()
        for i in range(5):
            history.store(make_reading("dev1", "temp", float(i), ts=base + i))

        recent = history.query_recent("dev1", "temp", limit=5)
        # Should be in chronological order (oldest first)
        assert recent[0]["value"] == 0.0
        assert recent[4]["value"] == 4.0


class TestLatest:
    def test_get_latest(self, history):
        base = time.time()
        history.store(make_reading("dev1", "temp", 25.0, ts=base))
        history.store(make_reading("dev1", "temp", 30.0, ts=base + 1))

        latest = history.get_latest("dev1", "temp")
        assert latest is not None
        assert latest["value"] == 30.0

    def test_get_latest_none(self, history):
        latest = history.get_latest("nonexistent", "sensor")
        assert latest is None


class TestStats:
    def test_get_stats(self, history):
        base = time.time()
        for v in [20.0, 25.0, 30.0, 35.0, 40.0]:
            history.store(make_reading("dev1", "temp", v, ts=base))

        stats = history.get_stats("dev1", "temp", window_seconds=3600)
        assert stats["count"] == 5
        assert stats["min"] == 20.0
        assert stats["max"] == 40.0
        assert stats["mean"] == 30.0
        assert stats["latest"] == 40.0

    def test_get_stats_empty(self, history):
        stats = history.get_stats("nonexistent", "sensor")
        assert stats["count"] == 0


class TestQueryRange:
    def test_query_range(self, history):
        base = time.time()
        for i in range(10):
            history.store(make_reading("dev1", "temp", 25.0 + i, ts=base + i))

        results = history.query_range("dev1", "temp", base, base + 10)
        assert len(results) == 10

    def test_query_range_empty(self, history):
        results = history.query_range("dev1", "temp", 0, time.time())
        assert len(results) == 0


class TestCompaction:
    def test_compact_old_readings(self, history):
        """Old readings should be compacted into 1-minute buckets."""
        # Set compaction cutoff to 0 hours so everything qualifies
        history.config.compaction_after_hours = 0

        base = time.time() - 7200  # 2 hours ago
        for i in range(120):
            history.store(make_reading("dev1", "temp", 25.0 + i * 0.1, ts=base + i))

        compacted = history.compact()
        assert compacted == 120

        # High-res readings should be gone
        recent = history.query_recent("dev1", "temp", limit=100)
        assert len(recent) == 0


class TestRetention:
    def test_retention_deletes_old(self, history):
        history.config.retention_days = 0  # Delete everything

        old_ts = time.time() - 86400 * 10  # 10 days ago
        history.store(make_reading("dev1", "temp", 25.0, ts=old_ts))
        history.store(make_reading("dev1", "temp", 30.0, ts=time.time()))

        deleted = history.enforce_retention()
        assert deleted >= 1

        recent = history.query_recent("dev1", "temp", limit=10)
        assert all(r["timestamp"] > old_ts + 86400 for r in recent)


class TestMaxHighRes:
    def test_enforce_max_high_res(self, history):
        history.config.max_high_res = 5

        base = time.time()
        for i in range(20):
            history.store(make_reading("dev1", "temp", float(i), ts=base + i))

        deleted = history.enforce_max_high_res()
        assert deleted == 15

        recent = history.query_recent("dev1", "temp", limit=100)
        assert len(recent) == 5


class TestDeviceAndSensorListing:
    def test_get_all_devices(self, history):
        history.store(make_reading("dev1", "temp", 25.0))
        history.store(make_reading("dev2", "temp", 30.0))

        devices = history.get_all_devices()
        assert "dev1" in devices
        assert "dev2" in devices

    def test_get_all_sensors(self, history):
        history.store(make_reading("dev1", "temp", 25.0))
        history.store(make_reading("dev1", "pressure", 3.0))

        sensors = history.get_all_sensors("dev1")
        assert "temp" in sensors
        assert "pressure" in sensors
