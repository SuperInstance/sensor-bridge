"""
MQTT Client — subscribes to ESP32 sensor data and feeds the bridge.

This is the network layer between the ESP32 ensigns and the sensor bridge.
It connects to the local MQTT broker, subscribes to sensor topics, and
passes raw messages to the normalizer → pattern detector → escalation chain.

Topic structure:
    vessel/{device_id}/sensors/{sensor_name} — raw readings
    vessel/{device_id}/alerts               — alert events (from ensign)
    vessel/{device_id}/status               — device heartbeat
    vessel/{device_id}/config               — config updates from LaForge

The client uses paho-mqtt for the MQTT protocol. It handles reconnection
automatically (the ensign's blinders — reconnect only between sensor reads,
never blocking the processing loop).

Lifecycle:
    1. Connect to broker
    2. Subscribe to vessel/+/sensors/+, vessel/+/status, vessel/+/alerts
    3. On message: parse → normalize → detect patterns → escalate → store
    4. On disconnect: auto-reconnect with backoff
    5. Publish heartbeat on vessel/bridge/status every 30s
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from .normalizer import Normalizer, SensorReading
from .pattern_detector import PatternDetector, PatternEvent
from .escalation import EscalationRouter, EscalationAction
from .history import SensorHistory

logger = logging.getLogger(__name__)

# paho-mqtt is imported lazily so the rest of the module works without it
try:
    import paho.mqtt.client as mqtt
    PAHO_AVAILABLE = True
except ImportError:
    PAHO_AVAILABLE = False
    mqtt = None  # type: ignore


class SensorBridgeMQTTClient:
    """
    MQTT client that connects ESP32 sensor data to the exocortex.

    Wraps paho-mqtt with the sensor bridge processing pipeline:
        MQTT message → normalize → pattern detect → escalate → store

    Can also run without a real MQTT broker (for testing/dev) by calling
    process_message() directly with fabricated messages.
    """

    def __init__(
        self,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        client_id: str = "sensor-bridge",
        keepalive: int = 60,
        topic_root: str = "vessel",
        username: str = "",
        password: str = "",
        normalizer: Normalizer | None = None,
        pattern_detector: PatternDetector | None = None,
        escalation_router: EscalationRouter | None = None,
        history: SensorHistory | None = None,
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self.keepalive = keepalive
        self.topic_root = topic_root
        self.username = username
        self.password = password

        self.normalizer = normalizer
        self.pattern_detector = pattern_detector
        self.escalation_router = escalation_router
        self.history = history

        # Pipeline callback: called with list[SensorReading] after normalization
        self.on_readings: Callable[[list[SensorReading]], None] | None = None
        # Pattern callback: called with list[PatternEvent] after detection
        self.on_patterns: Callable[[list[PatternEvent]], None] | None = None
        # Escalation callback: called with EscalationAction after routing
        self.on_escalation: Callable[[EscalationAction], None] | None = None

        self._client: Any = None  # paho.mqtt.client.Client
        self._connected = False
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False
        self._heartbeat_interval = 30  # seconds

        # Statistics
        self.messages_received = 0
        self.readings_processed = 0
        self.patterns_detected = 0
        self.escalations_fired = 0

    # --- Connection management ---

    def connect(self) -> None:
        """Connect to the MQTT broker and start the processing loop."""
        if not PAHO_AVAILABLE:
            raise RuntimeError(
                "paho-mqtt is not installed. Install with: pip install paho-mqtt"
            )

        self._client = mqtt.Client(client_id=self.client_id)

        if self.username:
            self._client.username_pw_set(self.username, self.password)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        logger.info(
            "Connecting to MQTT broker %s:%d as %s",
            self.broker_host, self.broker_port, self.client_id,
        )

        self._client.connect(self.broker_host, self.broker_port, self.keepalive)
        self._client.loop_start()
        self._running = True

        # Start heartbeat
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
        )
        self._heartbeat_thread.start()

    def disconnect(self) -> None:
        """Disconnect from the broker."""
        self._running = False

        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)

        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

        self._connected = False
        logger.info("Disconnected from MQTT broker")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- paho-mqtt callbacks ---

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: dict,
        rc: int,
    ) -> None:
        """Called when connected to the broker."""
        if rc == 0:
            self._connected = True
            logger.info("Connected to MQTT broker")

            # Subscribe to sensor topics
            topics = [
                f"{self.topic_root}/+/sensors/+",
                f"{self.topic_root}/+/status",
                f"{self.topic_root}/+/alerts",
            ]
            for topic in topics:
                client.subscribe(topic)
                logger.info("Subscribed to: %s", topic)
        else:
            logger.error("MQTT connection failed with code %d", rc)

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        rc: int,
    ) -> None:
        """Called when disconnected from the broker."""
        self._connected = False
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%d). Will auto-reconnect.", rc)
        else:
            logger.info("MQTT disconnected cleanly")

    def _on_message(
        self,
        client: Any,
        userdata: Any,
        msg: Any,
    ) -> None:
        """Called when an MQTT message arrives."""
        topic = msg.topic
        try:
            payload_raw = msg.payload.decode("utf-8")
        except Exception:
            logger.warning("Failed to decode payload on topic %s", topic)
            return

        # Try to parse as JSON, fall back to raw string
        try:
            payload: Any = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = payload_raw

        # Note: process_message handles its own counter increment
        self.process_message(topic, payload)

    # --- Processing pipeline ---

    def process_message(self, topic: str, payload: Any) -> None:
        """
        Process a single MQTT message through the full pipeline.

        This is the main entry point and can be called directly
        (bypassing MQTT) for testing.

        Pipeline:
            1. Parse topic → extract device_id, channel
            2. Normalize → SensorReading objects
            3. Pattern detect → PatternEvent objects
            4. Escalate → EscalationAction objects
            5. Store → persist to history
        """
        self.messages_received += 1

        # If payload is a string that looks like JSON, try to parse it
        if isinstance(payload, str):
            try:
                import json as _json
                payload = _json.loads(payload)
            except (ValueError, json.JSONDecodeError):
                pass  # Keep as raw string — normalizer handles it

        # 1. Normalize
        readings: list[SensorReading] = []
        if self.normalizer:
            readings = self.normalizer.normalize_topic(topic, payload)

        if not readings:
            return

        self.readings_processed += len(readings)

        if self.on_readings:
            self.on_readings(readings)

        # 2. Store in history
        if self.history:
            for reading in readings:
                self.history.store(reading)

        # 3. Pattern detection
        all_patterns: list[PatternEvent] = []
        if self.pattern_detector:
            for reading in readings:
                events = self.pattern_detector.check(reading)
                all_patterns.extend(events)

        if all_patterns:
            self.patterns_detected += len(all_patterns)
            if self.on_patterns:
                self.on_patterns(all_patterns)

        # 4. Escalation
        if self.escalation_router:
            for pattern in all_patterns:
                action = self.escalation_router.route(pattern)
                self.escalations_fired += 1
                if self.on_escalation:
                    self.on_escalation(action)

    # --- Heartbeat ---

    def _heartbeat_loop(self) -> None:
        """Publish bridge status every 30 seconds."""
        while self._running:
            if self._connected and self._client:
                status = {
                    "client": self.client_id,
                    "uptime": int(time.time()),
                    "messages_received": self.messages_received,
                    "readings_processed": self.readings_processed,
                    "patterns_detected": self.patterns_detected,
                    "escalations_fired": self.escalations_fired,
                }
                topic = f"{self.topic_root}/bridge/status"
                try:
                    self._client.publish(topic, json.dumps(status))
                except Exception:
                    logger.debug("Failed to publish heartbeat")

            time.sleep(self._heartbeat_interval)

    # --- Publishing (for LaForge config updates) ---

    def publish_config(
        self,
        device_id: str,
        config: dict[str, Any],
    ) -> None:
        """
        Publish a config update to a device (LaForge → ensign).

        Topic: vessel/{device_id}/config
        """
        if not self._client or not self._connected:
            logger.warning("Cannot publish: not connected")
            return

        topic = f"{self.topic_root}/{device_id}/config"
        self._client.publish(topic, json.dumps(config))
        logger.info("Published config update to %s", topic)

    def publish_alert(
        self,
        device_id: str,
        alert: dict[str, Any],
    ) -> None:
        """Publish an alert to a device's alert topic."""
        if not self._client or not self._connected:
            logger.warning("Cannot publish: not connected")
            return

        topic = f"{self.topic_root}/{device_id}/alerts"
        self._client.publish(topic, json.dumps(alert))
        logger.info("Published alert to %s", topic)

    # --- Status ---

    def get_status(self) -> dict[str, Any]:
        """Get current bridge status."""
        return {
            "connected": self._connected,
            "broker": f"{self.broker_host}:{self.broker_port}",
            "client_id": self.client_id,
            "messages_received": self.messages_received,
            "readings_processed": self.readings_processed,
            "patterns_detected": self.patterns_detected,
            "escalations_fired": self.escalations_fired,
        }
