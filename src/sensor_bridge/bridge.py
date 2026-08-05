"""
Sensor Bridge — main orchestrator.

Wires together all components: MQTT client → normalizer → pattern detector
→ escalation router → history. Provides a single entry point for running
the bridge.

Usage:
    from sensor_bridge import Bridge
    bridge = Bridge.from_config("config/sensor_bridge_config.yaml")
    bridge.run()  # Blocking — runs until interrupted

Or programmatically:
    bridge = Bridge(config)
    bridge.start()  # Non-blocking
    # ... do stuff ...
    bridge.stop()
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .config_loader import BridgeConfig, load_config
from .normalizer import Normalizer, SensorReading
from .pattern_detector import PatternDetector, PatternEvent
from .escalation import EscalationRouter, EscalationAction
from .history import SensorHistory
from .mqtt_client import SensorBridgeMQTTClient

logger = logging.getLogger(__name__)


class Bridge:
    """
    The sensor bridge orchestrator.

    Connects MQTT client to the processing pipeline and manages lifecycle.
    """

    def __init__(self, config: BridgeConfig):
        self.config = config

        # Initialize components
        self.normalizer = Normalizer(config.devices)
        self.pattern_detector = PatternDetector(
            device_configs=config.devices,
            rolling_window=config.pattern_detector.get("rolling_window", 60),
            spike_stddev=config.pattern_detector.get("spike_stddev", 4.0),
            drift_threshold=config.pattern_detector.get("drift_threshold", 2.0),
            stuck_check_enabled=config.pattern_detector.get(
                "stuck_check_enabled", True
            ),
        )
        self.history = SensorHistory(config.history_config)
        self.escalation_router = EscalationRouter(
            escalation_config=config.escalation,
        )

        # Initialize MQTT client
        self.mqtt_client = SensorBridgeMQTTClient(
            broker_host=config.broker_host,
            broker_port=config.broker_port,
            client_id=config.client_id,
            keepalive=config.keepalive,
            topic_root=config.topic_root,
            username=config.broker_username,
            password=config.broker_password,
            normalizer=self.normalizer,
            pattern_detector=self.pattern_detector,
            escalation_router=self.escalation_router,
            history=self.history,
        )

        # Wire up logging callbacks
        self.mqtt_client.on_readings = self._on_readings
        self.mqtt_client.on_patterns = self._on_patterns
        self.mqtt_client.on_escalation = self._on_escalation

        self._running = False

    @classmethod
    def from_config(cls, config_path: str | Path) -> Bridge:
        """Create a Bridge from a YAML config file."""
        config = load_config(config_path)
        return cls(config)

    def start(self) -> None:
        """Start the bridge (non-blocking). Connects to MQTT broker."""
        logger.info("Starting sensor bridge...")
        self.mqtt_client.connect()
        self._running = True
        logger.info("Sensor bridge started")

    def stop(self) -> None:
        """Stop the bridge gracefully."""
        logger.info("Stopping sensor bridge...")
        self._running = False
        self.mqtt_client.disconnect()
        self.history.close()
        logger.info("Sensor bridge stopped")

    def run(self) -> None:
        """Run the bridge (blocking). Handles SIGINT/SIGTERM."""
        self.start()

        def signal_handler(signum, frame):
            logger.info("Received signal %d, shutting down", signum)
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        logger.info("Bridge running. Press Ctrl+C to stop.")

        while self._running:
            time.sleep(1)

    # --- Pipeline callbacks ---

    def _on_readings(self, readings: list[SensorReading]) -> None:
        """Called when new readings are normalized."""
        for r in readings:
            logger.debug(
                "Reading: %s/%s = %.1f %s [%s]",
                r.device_id, r.sensor, r.value, r.unit, r.quality,
            )

    def _on_patterns(self, patterns: list[PatternEvent]) -> None:
        """Called when patterns are detected."""
        for p in patterns:
            logger.info(
                "Pattern: %s/%s %s (severity=%d) — %s",
                p.device_id, p.sensor,
                p.pattern_type.name, p.severity, p.message,
            )

    def _on_escalation(self, action: EscalationAction) -> None:
        """Called when an escalation action is triggered."""
        logger.warning(
            "ESCALATION [%s]: %s — actions: %s",
            action.level_name, action.pattern.message,
            "; ".join(action.actions),
        )

    # --- Manual injection (for testing/CLI) ---

    def inject_reading(
        self,
        device_id: str,
        sensor: str,
        value: float,
    ) -> list[PatternEvent]:
        """
        Manually inject a sensor reading (bypasses MQTT).

        Useful for testing the pipeline without an ESP32.
        """
        reading = self.normalizer.normalize(device_id, sensor, value)
        if not reading:
            return []

        # Store
        self.history.store(reading)

        # Detect patterns
        events = self.pattern_detector.check(reading)

        # Escalate
        for event in events:
            self.escalation_router.route(event)

        return events

    def get_status(self) -> dict[str, Any]:
        """Get full bridge status."""
        status = self.mqtt_client.get_status()
        status["escalation"] = self.escalation_router.get_escalation_summary()
        status["devices"] = self.normalizer.known_devices()
        return status


def main():
    """CLI entry point — run the sensor bridge."""
    import argparse

    parser = argparse.ArgumentParser(description="Sensor Bridge")
    parser.add_argument(
        "--config", "-c",
        default="src/sensor_bridge/config.yaml",
        help="Path to config file",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bridge = Bridge.from_config(args.config)
    bridge.run()


if __name__ == "__main__":
    main()
