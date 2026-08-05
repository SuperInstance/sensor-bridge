"""
History — time-series storage for sensor data.

Stores every normalized sensor reading in a SQLite database with
time-series optimization. Provides query methods for retrieving
recent data, computing statistics, and feeding the pattern detector.

Storage strategy (tiered):
    Recent (high-res):  Every reading, 1-second resolution, in SQLite.
    Compacted (1-min):  After 24 hours, readings are merged into 1-minute
                        averages (min, max, mean, count).
    Archive (raw):      After retention_days, compacted data is deleted.

This mirrors the exocortex's storage philosophy: recent data in high-res,
historical data in summary form. The pattern detector needs recent detail;
LaForge needs historical trends.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .normalizer import SensorReading

logger = logging.getLogger(__name__)


@dataclass
class HistoryConfig:
    """Configuration for the history store."""
    db_path: str = "data/sensor_history.db"
    retention_days: int = 90
    compaction_after_hours: int = 24
    max_high_res: int = 100_000


class SensorHistory:
    """
    Time-series storage for sensor readings using SQLite.

    The database has two tables per sensor stream:
        - readings:     High-resolution (every reading)
        - compacted:    1-minute aggregates (min, max, mean, count)

    Schema:
        readings(device_id TEXT, sensor TEXT, timestamp REAL,
                 value REAL, unit TEXT, quality TEXT)

        compacted(device_id TEXT, sensor TEXT, bucket_start REAL,
                  bucket_end REAL, min_val REAL, max_val REAL,
                  mean_val REAL, count INTEGER)
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS readings (
        device_id   TEXT NOT NULL,
        sensor      TEXT NOT NULL,
        timestamp   REAL NOT NULL,
        value       REAL NOT NULL,
        unit        TEXT NOT NULL DEFAULT '',
        quality     TEXT NOT NULL DEFAULT 'good'
    );

    CREATE INDEX IF NOT EXISTS idx_readings_lookup
        ON readings(device_id, sensor, timestamp DESC);

    CREATE INDEX IF NOT EXISTS idx_readings_time
        ON readings(timestamp);

    CREATE TABLE IF NOT EXISTS compacted (
        device_id     TEXT NOT NULL,
        sensor        TEXT NOT NULL,
        bucket_start  REAL NOT NULL,
        bucket_end    REAL NOT NULL,
        min_val       REAL NOT NULL,
        max_val       REAL NOT NULL,
        mean_val      REAL NOT NULL,
        count         INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_compacted_lookup
        ON compacted(device_id, sensor, bucket_start DESC);
    """

    def __init__(self, config: HistoryConfig | None = None):
        self.config = config or HistoryConfig()
        self.db_path = Path(self.config.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
        logger.info("Sensor history initialized: %s", self.db_path)

    def store(self, reading: SensorReading) -> None:
        """Store a single sensor reading."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO readings
                   (device_id, sensor, timestamp, value, unit, quality)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    reading.device_id,
                    reading.sensor,
                    reading.timestamp,
                    reading.value,
                    reading.unit,
                    reading.quality,
                ),
            )

    def store_batch(self, readings: list[SensorReading]) -> int:
        """Store multiple readings efficiently. Returns count stored."""
        if not readings:
            return 0
        rows = [
            (r.device_id, r.sensor, r.timestamp, r.value, r.unit, r.quality)
            for r in readings
        ]
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO readings
                   (device_id, sensor, timestamp, value, unit, quality)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def query_recent(
        self,
        device_id: str,
        sensor: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get the most recent readings for a sensor.

        Returns list of dicts with: timestamp, value, unit, quality.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """SELECT timestamp, value, unit, quality
                   FROM readings
                   WHERE device_id = ? AND sensor = ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (device_id, sensor, limit),
            )
            rows = cursor.fetchall()

        return [
            {
                "timestamp": row["timestamp"],
                "value": row["value"],
                "unit": row["unit"],
                "quality": row["quality"],
            }
            for row in reversed(rows)  # Chronological order
        ]

    def query_range(
        self,
        device_id: str,
        sensor: str,
        start_time: float,
        end_time: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query readings in a time range.

        Uses compacted data for ranges older than the compaction threshold,
        high-res data for recent ranges.
        """
        end = end_time or time.time()
        results: list[dict[str, Any]] = []
        compaction_cutoff = time.time() - (self.config.compaction_after_hours * 3600)

        with self._connect() as conn:
            # High-res data for recent range
            if end > compaction_cutoff:
                cursor = conn.execute(
                    """SELECT timestamp, value, unit, quality
                       FROM readings
                       WHERE device_id = ? AND sensor = ?
                         AND timestamp >= ? AND timestamp <= ?
                       ORDER BY timestamp ASC""",
                    (device_id, sensor,
                     max(start_time, compaction_cutoff), end),
                )
                for row in cursor.fetchall():
                    results.append({
                        "timestamp": row["timestamp"],
                        "value": row["value"],
                        "unit": row["unit"],
                        "quality": row["quality"],
                    })

            # Compacted data for older range
            if start_time < compaction_cutoff:
                cursor = conn.execute(
                    """SELECT bucket_start, bucket_end, min_val, max_val,
                              mean_val, count
                       FROM compacted
                       WHERE device_id = ? AND sensor = ?
                         AND bucket_start >= ? AND bucket_end <= ?
                       ORDER BY bucket_start ASC""",
                    (device_id, sensor, start_time, compaction_cutoff),
                )
                for row in cursor.fetchall():
                    results.append({
                        "timestamp": row["bucket_start"],
                        "value": row["mean_val"],
                        "unit": "",
                        "quality": "compacted",
                        "min": row["min_val"],
                        "max": row["max_val"],
                        "count": row["count"],
                    })

        return results

    def get_stats(
        self,
        device_id: str,
        sensor: str,
        window_seconds: int = 3600,
    ) -> dict[str, float | int]:
        """
        Get statistics for a sensor over a recent time window.

        Returns: count, min, max, mean, latest.
        """
        cutoff = time.time() - window_seconds
        with self._connect() as conn:
            cursor = conn.execute(
                """SELECT COUNT(*) as count,
                          MIN(value) as min_val,
                          MAX(value) as max_val,
                          AVG(value) as mean_val
                   FROM readings
                   WHERE device_id = ? AND sensor = ? AND timestamp >= ?""",
                (device_id, sensor, cutoff),
            )
            row = cursor.fetchone()
            if not row or row["count"] == 0:
                return {"count": 0, "min": 0, "max": 0, "mean": 0, "latest": 0}
            cursor = conn.execute(
                """SELECT value FROM readings
                   WHERE device_id = ? AND sensor = ? AND timestamp >= ?
                   ORDER BY timestamp DESC, rowid DESC LIMIT 1""",
                (device_id, sensor, cutoff),
            )
            latest_row = cursor.fetchone()

        return {
            "count": row["count"],
            "min": round(row["min_val"], 3),
            "max": round(row["max_val"], 3),
            "mean": round(row["mean_val"], 3),
            "latest": round(latest_row["value"], 3) if latest_row else 0,
        }

    def get_latest(
        self,
        device_id: str,
        sensor: str,
    ) -> dict[str, Any] | None:
        """Get the single most recent reading for a sensor."""
        with self._connect() as conn:
            cursor = conn.execute(
                """SELECT timestamp, value, unit, quality
                   FROM readings
                   WHERE device_id = ? AND sensor = ?
                   ORDER BY timestamp DESC
                   LIMIT 1""",
                (device_id, sensor),
            )
            row = cursor.fetchone()

        if not row:
            return None

        return {
            "timestamp": row["timestamp"],
            "value": row["value"],
            "unit": row["unit"],
            "quality": row["quality"],
        }

    def compact(self) -> int:
        """
        Compact old high-res readings into 1-minute aggregates.

        Merges readings older than compaction_after_hours into
        1-minute buckets in the compacted table, then deletes
        the original high-res rows.

        Returns: number of rows compacted.
        """
        cutoff = time.time() - (self.config.compaction_after_hours * 3600)
        bucket_seconds = 60  # 1-minute buckets

        compacted_count = 0

        with self._connect() as conn:
            # Find all (device, sensor) pairs with old readings
            cursor = conn.execute(
                """SELECT DISTINCT device_id, sensor
                   FROM readings WHERE timestamp < ?""",
                (cutoff,),
            )
            streams = cursor.fetchall()

            for stream in streams:
                device_id = stream["device_id"]
                sensor = stream["sensor"]

                # Get old readings ordered by time
                cursor = conn.execute(
                    """SELECT timestamp, value
                       FROM readings
                       WHERE device_id = ? AND sensor = ? AND timestamp < ?
                       ORDER BY timestamp ASC""",
                    (device_id, sensor, cutoff),
                )
                readings = cursor.fetchall()

                if not readings:
                    continue

                # Group into 1-minute buckets
                buckets: dict[int, list[tuple[float, float]]] = {}
                for r in readings:
                    bucket_start = int(r["timestamp"] // bucket_seconds) * bucket_seconds
                    buckets.setdefault(bucket_start, []).append(
                        (r["timestamp"], r["value"])
                    )

                # Insert compacted entries
                for bucket_start, entries in buckets.items():
                    values = [v for _, v in entries]
                    timestamps = [t for t, _ in entries]
                    conn.execute(
                        """INSERT OR REPLACE INTO compacted
                           (device_id, sensor, bucket_start, bucket_end,
                            min_val, max_val, mean_val, count)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            device_id, sensor,
                            bucket_start,
                            max(timestamps),
                            min(values),
                            max(values),
                            sum(values) / len(values),
                            len(values),
                        ),
                    )
                    compacted_count += len(entries)

                # Delete compacted high-res rows
                conn.execute(
                    """DELETE FROM readings
                       WHERE device_id = ? AND sensor = ? AND timestamp < ?""",
                    (device_id, sensor, cutoff),
                )

        if compacted_count:
            logger.info(
                "Compacted %d readings into %d buckets",
                compacted_count, len(buckets) if "buckets" in dir() else 0,
            )

        return compacted_count

    def enforce_retention(self) -> int:
        """
        Delete data older than retention_days.

        Returns: number of rows deleted.
        """
        cutoff = time.time() - (self.config.retention_days * 86400)
        deleted = 0

        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM readings WHERE timestamp < ?", (cutoff,)
            )
            deleted += cursor.rowcount

            cursor = conn.execute(
                "DELETE FROM compacted WHERE bucket_start < ?", (cutoff,)
            )
            deleted += cursor.rowcount

        if deleted:
            logger.info("Retention: deleted %d old rows", deleted)

        return deleted

    def enforce_max_high_res(self) -> int:
        """
        Enforce the max_high_res limit by deleting oldest readings.

        Keeps the most recent max_high_res readings per (device, sensor).
        Returns: number of rows deleted.
        """
        deleted = 0

        with self._connect() as conn:
            cursor = conn.execute(
                """SELECT DISTINCT device_id, sensor FROM readings"""
            )
            streams = cursor.fetchall()

            for stream in streams:
                cursor = conn.execute(
                    """DELETE FROM readings
                       WHERE device_id = ? AND sensor = ?
                         AND timestamp NOT IN (
                           SELECT timestamp FROM readings
                           WHERE device_id = ? AND sensor = ?
                           ORDER BY timestamp DESC
                           LIMIT ?
                       )""",
                    (
                        stream["device_id"], stream["sensor"],
                        stream["device_id"], stream["sensor"],
                        self.config.max_high_res,
                    ),
                )
                deleted += cursor.rowcount

        if deleted:
            logger.info("Max high-res: pruned %d old rows", deleted)

        return deleted

    def get_all_devices(self) -> list[str]:
        """Get list of all device IDs that have data."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT device_id FROM readings"
            )
            return [row["device_id"] for row in cursor.fetchall()]

    def get_all_sensors(self, device_id: str) -> list[str]:
        """Get list of all sensor names for a device."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT sensor FROM readings WHERE device_id = ?",
                (device_id,),
            )
            return [row["sensor"] for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the database connection (cleanup)."""
        # Connections are opened/closed per-operation, so nothing to do here.
        pass
