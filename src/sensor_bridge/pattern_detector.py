"""
Pattern Detector — detects anomalies, trends, and threshold crossings.

This is the exocortex's sensory cortex. It watches the stream of normalized
sensor readings and identifies patterns that matter:

    - Threshold crossings (value exceeds configured warning/critical limits)
    - Spikes (sudden jumps that exceed N standard deviations from rolling mean)
    - Drift (gradual movement in one direction over time)
    - Stuck values (sensor hasn't changed — likely disconnected or frozen)

When a pattern is detected, it emits a PatternEvent that the escalation
module uses to decide who to notify and how urgently.

Design principle (from RACEHORSES_WITH_BLINDERS):
    The pattern detector is the ensign's reflex — it matches sensor readings
    against known patterns and responds in milliseconds. It doesn't deliberate
    about WHY a pattern is happening. That's LaForge's job.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import Any, Literal

from .normalizer import SensorReading

logger = logging.getLogger(__name__)


class PatternType(IntEnum):
    THRESHOLD_WARNING = 1
    THRESHOLD_CRITICAL = 2
    SPIKE = 3
    DRIFT = 4
    STUCK = 5
    RECOVERY = 6  # Value returned to normal after being abnormal


class Severity(IntEnum):
    """Escalation severity — maps to the 4-level protocol."""
    NORMAL = 0      # Level 0 — ensign handles
    WARNING = 1     # Level 1 — log for LaForge
    ALERT = 2       # Level 2 — notify captain, page LaForge
    CRITICAL = 3    # Level 3 — all hands


@dataclass
class PatternEvent:
    """A detected pattern in sensor data."""
    device_id: str
    sensor: str
    pattern_type: PatternType
    severity: Severity
    value: float
    expected: float | None = None  # What we expected (e.g. rolling mean for spikes)
    threshold: float | None = None  # Which threshold was crossed
    message: str = ""
    timestamp: float = field(default_factory=time.time)
    reading: SensorReading | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Remove nested reading for logging (it's kept separately)
        d.pop("reading", None)
        d["pattern_type"] = self.pattern_type.name
        d["severity"] = int(self.severity)
        return d


class _SensorWindow:
    """Rolling window of recent readings for a single sensor stream."""

    def __init__(self, window_size: int = 60):
        self.values: deque[float] = deque(maxlen=window_size)
        self.timestamps: deque[float] = deque(maxlen=window_size)
        self.last_alert_state: Severity = Severity.NORMAL

    def add(self, value: float, timestamp: float) -> None:
        self.values.append(value)
        self.timestamps.append(timestamp)

    @property
    def count(self) -> int:
        return len(self.values)

    def mean(self) -> float:
        if not self.values:
            return 0.0
        return sum(self.values) / len(self.values)

    def stddev(self) -> float:
        if len(self.values) < 2:
            return 0.0
        m = self.mean()
        variance = sum((v - m) ** 2 for v in self.values) / len(self.values)
        return variance ** 0.5

    def recent_mean(self, n: int = 10) -> float:
        """Mean of the last n readings."""
        if not self.values:
            return 0.0
        recent = list(self.values)[-n:]
        return sum(recent) / len(recent)

    def rate_of_change(self) -> float:
        """Instantaneous rate of change (value/sec) between last two readings."""
        if len(self.values) < 2 or len(self.timestamps) < 2:
            return 0.0
        dt = self.timestamps[-1] - self.timestamps[-2]
        if dt == 0:
            return 0.0
        return (self.values[-1] - self.values[-2]) / dt

    def is_stuck(self, band: float, min_readings: int) -> bool:
        """Check if values haven't moved outside a band in min_readings."""
        if len(self.values) < min_readings:
            return False
        recent = list(self.values)[-min_readings:]
        val_range = max(recent) - min(recent)
        return val_range <= band

    def drift_rate(self, window_seconds: float = 60.0) -> float:
        """
        Estimate drift: slope of linear regression over recent readings
        within the time window. Returns units per second.
        """
        if len(self.values) < 3:
            return 0.0
        now = self.timestamps[-1]
        # Filter to readings within window
        x_vals: list[float] = []
        y_vals: list[float] = []
        for i, ts in enumerate(self.timestamps):
            if now - ts <= window_seconds:
                x_vals.append(ts - self.timestamps[0])
                y_vals.append(self.values[i])
        if len(x_vals) < 3:
            return 0.0
        n = len(x_vals)
        sum_x = sum(x_vals)
        sum_y = sum(y_vals)
        sum_xy = sum(x * y for x, y in zip(x_vals, y_vals))
        sum_x2 = sum(x * x for x in x_vals)
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return 0.0
        slope = (n * sum_xy - sum_x * sum_y) / denom
        return slope  # units per second


class PatternDetector:
    """
    Watches sensor readings and detects patterns.

    Maintains rolling windows per (device, sensor) stream and applies
    threshold, spike, drift, and stuck-value checks on each new reading.
    """

    def __init__(
        self,
        device_configs: dict[str, Any],
        rolling_window: int = 60,
        spike_stddev: float = 4.0,
        drift_threshold: float = 2.0,
        stuck_check_enabled: bool = True,
    ):
        self.device_configs = device_configs
        self.rolling_window = rolling_window
        self.spike_stddev = spike_stddev
        self.drift_threshold = drift_threshold
        self.stuck_check_enabled = stuck_check_enabled
        # Per-stream state: {(device_id, sensor): _SensorWindow}
        self._windows: dict[tuple[str, str], _SensorWindow] = {}

    def _get_window(self, device_id: str, sensor: str) -> _SensorWindow:
        key = (device_id, sensor)
        if key not in self._windows:
            self._windows[key] = _SensorWindow(self.rolling_window)
        return self._windows[key]

    def _get_sensor_config(self, device_id: str, sensor: str) -> dict:
        return (
            self.device_configs
            .get(device_id, {})
            .get("sensors", {})
            .get(sensor, {})
        )

    def check(self, reading: SensorReading) -> list[PatternEvent]:
        """
        Run all pattern checks on a new reading.

        Returns a list of PatternEvents (may be empty if all is normal).
        A single reading could trigger multiple patterns simultaneously
        (e.g., threshold crossing AND spike).
        """
        if reading.quality == "bad":
            return []

        events: list[PatternEvent] = []
        window = self._get_window(reading.device_id, reading.sensor)
        sensor_config = self._get_sensor_config(reading.device_id, reading.sensor)
        prev_state = window.last_alert_state

        # Add the new reading to the window
        window.add(reading.value, reading.timestamp)

        # 1. Threshold check
        threshold_event = self._check_threshold(reading, sensor_config, window)
        if threshold_event:
            events.append(threshold_event)

        # 2. Spike check
        spike_event = self._check_spike(reading, window, sensor_config)
        if spike_event:
            events.append(spike_event)

        # 3. Drift check
        drift_event = self._check_drift(reading, window, sensor_config)
        if drift_event:
            events.append(drift_event)

        # 4. Stuck value check
        if self.stuck_check_enabled:
            stuck_event = self._check_stuck(reading, window, sensor_config)
            if stuck_event:
                events.append(stuck_event)

        # 5. Recovery detection (value came back to normal)
        current_severity = max(
            (e.severity for e in events), default=Severity.NORMAL
        )
        if prev_state >= Severity.WARNING and current_severity == Severity.NORMAL:
            events.append(PatternEvent(
                device_id=reading.device_id,
                sensor=reading.sensor,
                pattern_type=PatternType.RECOVERY,
                severity=Severity.NORMAL,
                value=reading.value,
                message=f"{reading.sensor} returned to normal range",
                reading=reading,
            ))

        # Update state
        window.last_alert_state = current_severity

        return events

    def _check_threshold(
        self,
        reading: SensorReading,
        config: dict,
        window: _SensorWindow,
    ) -> PatternEvent | None:
        """Check if value crosses configured warning/critical thresholds."""
        thresholds = config.get("thresholds", {})
        value = reading.value

        # Handle conditional thresholds (e.g., oil pressure only when engine running)
        condition = thresholds.get("condition")
        if condition and not self._evaluate_condition(condition, reading.device_id):
            return None

        is_below = thresholds.get("below", False)

        # Determine critical and warning values
        if is_below:
            critical_val = thresholds.get("critical")
            warning_val = thresholds.get("warning")
            crossed_critical = critical_val is not None and value <= critical_val
            crossed_warning = warning_val is not None and value <= warning_val
        else:
            critical_val = thresholds.get("critical")
            warning_val = thresholds.get("warning")
            crossed_critical = critical_val is not None and value >= critical_val
            crossed_warning = warning_val is not None and value >= warning_val

        # Also check paired low/high thresholds (e.g., battery voltage)
        critical_low = thresholds.get("critical_low")
        warning_low = thresholds.get("warning_low")
        critical_high = thresholds.get("critical_high")
        warning_high = thresholds.get("warning_high")

        if critical_low is not None and value <= critical_low:
            crossed_critical = True
            critical_val = critical_low
        elif critical_high is not None and value >= critical_high:
            crossed_critical = True
            critical_val = critical_high

        if warning_low is not None and value <= warning_low:
            crossed_warning = True
            warning_val = warning_low
        elif warning_high is not None and value >= warning_high:
            crossed_warning = True
            warning_val = warning_high

        if crossed_critical:
            return PatternEvent(
                device_id=reading.device_id,
                sensor=reading.sensor,
                pattern_type=PatternType.THRESHOLD_CRITICAL,
                severity=Severity.CRITICAL,
                value=value,
                threshold=critical_val,
                message=f"{reading.sensor} critical: {value:.1f}{reading.unit} (threshold: {critical_val})",
                reading=reading,
            )

        if crossed_warning:
            return PatternEvent(
                device_id=reading.device_id,
                sensor=reading.sensor,
                pattern_type=PatternType.THRESHOLD_WARNING,
                severity=Severity.WARNING,
                value=value,
                threshold=warning_val,
                message=f"{reading.sensor} warning: {value:.1f}{reading.unit} (threshold: {warning_val})",
                reading=reading,
            )

        return None

    def _check_spike(
        self,
        reading: SensorReading,
        window: _SensorWindow,
        config: dict,
    ) -> PatternEvent | None:
        """Detect sudden jumps exceeding N standard deviations from rolling mean."""
        if window.count < 5:
            return None  # Not enough data

        anomaly_config = config.get("anomaly", {})
        max_roc = anomaly_config.get("max_rate_of_change")
        if max_roc is not None:
            roc = abs(window.rate_of_change())
            if roc > max_roc:
                return PatternEvent(
                    device_id=reading.device_id,
                    sensor=reading.sensor,
                    pattern_type=PatternType.SPIKE,
                    severity=Severity.WARNING,
                    value=reading.value,
                    expected=window.mean(),
                    message=(
                        f"{reading.sensor} spike: rate of change {roc:.2f}/s "
                        f"exceeds max {max_roc:.2f}/s"
                    ),
                    reading=reading,
                )

        # Also check stddev-based spike
        std = window.stddev()
        if std > 0.01:  # Guard against near-zero stddev
            deviation = abs(reading.value - window.mean())
            # Only flag as spike if both: exceeds N stddev AND absolute deviation
            # is meaningful (avoids false spikes on sensors that have been very flat)
            if deviation > self.spike_stddev * std and deviation > 1.0:
                return PatternEvent(
                    device_id=reading.device_id,
                    sensor=reading.sensor,
                    pattern_type=PatternType.SPIKE,
                    severity=Severity.WARNING,
                    value=reading.value,
                    expected=window.mean(),
                    message=(
                        f"{reading.sensor} spike: {reading.value:.1f}{reading.unit} "
                        f"is {deviation/std:.1f}σ from mean"
                    ),
                    reading=reading,
                )

        return None

    def _check_drift(
        self,
        reading: SensorReading,
        window: _SensorWindow,
        config: dict,
    ) -> PatternEvent | None:
        """Detect gradual drift in one direction."""
        if window.count < 10:
            return None

        anomaly_config = config.get("anomaly", {})
        configured_drift = anomaly_config.get("drift_rate")
        drift_threshold = configured_drift or self.drift_threshold

        # drift_rate is configured as units per minute, convert to per second
        drift_per_sec = drift_threshold / 60.0
        actual_drift = window.drift_rate()

        if abs(actual_drift) > drift_per_sec:
            direction = "up" if actual_drift > 0 else "down"
            return PatternEvent(
                device_id=reading.device_id,
                sensor=reading.sensor,
                pattern_type=PatternType.DRIFT,
                severity=Severity.WARNING,
                value=reading.value,
                message=(
                    f"{reading.sensor} drifting {direction}: "
                    f"{abs(actual_drift) * 60:.2f}{reading.unit}/min "
                    f"(threshold: {drift_threshold:.2f})"
                ),
                reading=reading,
            )

        return None

    def _check_stuck(
        self,
        reading: SensorReading,
        window: _SensorWindow,
        config: dict,
    ) -> PatternEvent | None:
        """Detect stuck/frozen sensor values."""
        anomaly_config = config.get("anomaly", {})
        stuck_band = anomaly_config.get("stuck_band", 0.1)
        stuck_readings = anomaly_config.get("stuck_readings", 30)

        if window.is_stuck(stuck_band, stuck_readings):
            return PatternEvent(
                device_id=reading.device_id,
                sensor=reading.sensor,
                pattern_type=PatternType.STUCK,
                severity=Severity.WARNING,
                value=reading.value,
                message=(
                    f"{reading.sensor} appears stuck: value hasn't changed "
                    f"more than {stuck_band} in {stuck_readings} readings"
                ),
                reading=reading,
            )

        return None

    def _evaluate_condition(self, condition: str, device_id: str) -> bool:
        """
        Evaluate a simple threshold condition like 'rpm > 500'.

        Looks up the most recent reading for the referenced sensor
        on the same device.
        """
        try:
            # Parse: "rpm > 500"
            parts = condition.split()
            if len(parts) != 3:
                return True  # Can't parse — assume condition is met
            ref_sensor, op, ref_value = parts
            ref_val = float(ref_value)

            # Find the most recent reading for ref_sensor on this device
            key = (device_id, ref_sensor)
            window = self._windows.get(key)
            if not window or window.count == 0:
                return False  # No data for the reference sensor

            actual = window.values[-1]

            if op == ">":
                return actual > ref_val
            elif op == "<":
                return actual < ref_val
            elif op == ">=":
                return actual >= ref_val
            elif op == "<=":
                return actual <= ref_val
            elif op == "==":
                return actual == ref_val
            else:
                return True
        except (ValueError, IndexError):
            logger.warning("Cannot evaluate condition: %s", condition)
            return True

    def get_state(self, device_id: str, sensor: str) -> dict[str, Any]:
        """Get current state of a sensor stream (for status queries)."""
        window = self._windows.get((device_id, sensor))
        if not window:
            return {"device": device_id, "sensor": sensor, "status": "no_data"}
        return {
            "device": device_id,
            "sensor": sensor,
            "count": window.count,
            "mean": round(window.mean(), 2),
            "stddev": round(window.stddev(), 2),
            "last_value": window.values[-1] if window.values else None,
            "alert_state": int(window.last_alert_state),
        }
