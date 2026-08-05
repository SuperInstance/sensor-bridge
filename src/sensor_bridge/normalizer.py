"""
Normalizer — converts raw sensor readings into a standard format.

Regardless of sensor type (NMEA2000, analog, I2C, digital), every reading
becomes a SensorReading dataclass with consistent fields. This is the
contract between the physical world and the exocortex.

Standard reading format:
    {
        "device_id": "engine_ensign_1",
        "sensor": "coolant_temp",
        "value": 87.3,
        "unit": "°C",
        "timestamp": "2026-08-04T16:20:00Z",
        "quality": "good",          # good | suspect | bad
        "raw": {...}                 # Original payload, preserved
    }
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

logger = logging.getLogger(__name__)

Quality = Literal["good", "suspect", "bad"]


@dataclass
class SensorReading:
    """Normalized sensor reading — the universal data contract."""

    device_id: str
    sensor: str
    value: float
    unit: str
    timestamp: float  # Unix epoch seconds (UTC)
    quality: Quality = "good"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def iso_timestamp(self) -> str:
        """ISO 8601 timestamp."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SensorReading:
        return cls(
            device_id=d["device_id"],
            sensor=d["sensor"],
            value=float(d["value"]),
            unit=d.get("unit", ""),
            timestamp=float(d.get("timestamp", time.time())),
            quality=d.get("quality", "good"),
            raw=d.get("raw", {}),
        )


class Normalizer:
    """
    Normalizes raw MQTT payloads into SensorReading objects.

    Each ESP32 device publishes in its own format (depending on firmware).
    The normalizer maps device-specific formats to the standard SensorReading
    contract. Device mappings come from config.yaml.
    """

    def __init__(self, device_configs: dict[str, Any]):
        """
        Args:
            device_configs: The 'devices' section from config.yaml.
        """
        self.devices = device_configs
        # Build sensor lookup: {device_id: {sensor_name: sensor_config}}
        self._sensor_map: dict[str, dict[str, dict]] = {}
        for device_id, device_def in self.devices.items():
            self._sensor_map[device_id] = device_def.get("sensors", {})

    def normalize(
        self,
        device_id: str,
        sensor_name: str,
        raw_value: float | str | dict,
        timestamp: float | None = None,
    ) -> SensorReading | None:
        """
        Normalize a single sensor reading.

        Args:
            device_id: Device identifier (e.g. "engine_ensign_1").
            sensor_name: Sensor name (e.g. "coolant_temp").
            raw_value: The raw sensor value from MQTT.
            timestamp: Optional timestamp; defaults to now.

        Returns:
            SensorReading or None if device/sensor unknown.
        """
        if device_id not in self._sensor_map:
            logger.warning("Unknown device: %s", device_id)
            return None

        sensor_config = self._sensor_map[device_id].get(sensor_name)
        if not sensor_config:
            logger.warning("Unknown sensor %s on device %s", sensor_name, device_id)
            return None

        ts = timestamp or time.time()

        # Parse value — handle string, float, or dict payloads
        if isinstance(raw_value, dict):
            # Some firmwares send {"value": 87.3, "unit": "°C"}
            value = float(raw_value.get("value", raw_value.get("v", 0)))
            unit = raw_value.get("unit", sensor_config.get("unit", ""))
            raw = raw_value
        elif isinstance(raw_value, str):
            try:
                value = float(raw_value)
            except ValueError:
                logger.warning("Cannot parse value '%s' as float", raw_value)
                return None
            unit = sensor_config.get("unit", "")
            raw = {"raw_value": raw_value}
        else:
            value = float(raw_value)
            unit = sensor_config.get("unit", "")
            raw = {"value": raw_value}

        # Range check — mark quality
        quality: Quality = "good"
        min_val = sensor_config.get("min")
        max_val = sensor_config.get("max")
        if min_val is not None and value < min_val:
            quality = "suspect"
            logger.debug(
                "Value %.2f below min %.2f for %s/%s",
                value, min_val, device_id, sensor_name,
            )
        if max_val is not None and value > max_val:
            quality = "suspect"
            logger.debug(
                "Value %.2f above max %.2f for %s/%s",
                value, max_val, device_id, sensor_name,
            )

        # NaN / infinity check
        if value != value:  # NaN check
            quality = "bad"
        elif abs(value) == float("inf"):
            quality = "bad"

        return SensorReading(
            device_id=device_id,
            sensor=sensor_name,
            value=value,
            unit=unit,
            timestamp=ts,
            quality=quality,
            raw=raw,
        )

    def normalize_topic(
        self,
        topic: str,
        payload: dict[str, Any] | str | float,
    ) -> list[SensorReading]:
        """
        Normalize an MQTT message by parsing the topic structure.

        Topic format: vessel/{device_id}/sensors/{sensor_name}
        Also handles batch payloads where one message contains multiple sensors.

        Returns a list because a single status message may contain
        multiple sensor readings (e.g., the engine ensign's STATUS
        command returns RPM, temp, oil, volts, fuel_rate in one JSON).
        """
        parts = topic.strip("/").split("/")
        readings: list[SensorReading] = []

        if len(parts) < 3:
            logger.debug("Topic too short to parse: %s", topic)
            return readings

        # vessel/{device_id}/sensors/{sensor_name}  (4 parts)
        # vessel/{device_id}/status                 (3 parts, batch)
        # vessel/{device_id}/alerts                 (3 parts)
        device_id = parts[1]
        channel = parts[2]

        if channel == "sensors" and len(parts) >= 4:
            sensor_name = parts[3]
            reading = self.normalize(device_id, sensor_name, payload)
            if reading:
                readings.append(reading)

        elif channel == "status":
            # Batch payload: JSON dict with multiple sensor values
            if isinstance(payload, dict):
                for sensor_name, value in payload.items():
                    # Skip metadata keys
                    if sensor_name in ("device", "firmware", "uptime", "timestamp"):
                        continue
                    reading = self.normalize(device_id, sensor_name, value)
                    if reading:
                        readings.append(reading)

        return readings

    def known_devices(self) -> list[str]:
        """Return list of registered device IDs."""
        return list(self._sensor_map.keys())

    def known_sensors(self, device_id: str) -> list[str]:
        """Return list of sensor names for a device."""
        return list(self._sensor_map.get(device_id, {}).keys())
