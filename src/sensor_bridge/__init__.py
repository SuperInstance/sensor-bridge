"""
Sensor Bridge — connects real hardware devices to the exocortex.

Architecture:
    ESP32 reads sensors → MQTT to local broker → sensor_bridge subscribes
    → normalizer standardizes → pattern_detector checks → history stores
    → escalation routes to ensign/LaForge/captain

The bridge is the middleware between the physical world (sensors on ESP32s)
and the cognitive world (the exocortex memory layer, Wesley, LaForge).

Topic Structure:
    vessel/{device_id}/sensors/{sensor_name} — raw readings
    vessel/{device_id}/alerts               — alert events
    vessel/{device_id}/status               — device heartbeat
    vessel/{device_id}/config               — config updates from LaForge

Escalation Levels:
    0 - Normal   — ensign handles, no notification
    1 - Warning  — ensign handles, logs for LaForge's next review
    2 - Alert    — ensign handles, notifies captain, pages LaForge
    3 - Critical — ensign handles, all hands notified, LaForge invoked
"""

__version__ = "0.1.0"
__all__ = [
    "mqtt_client",
    "normalizer",
    "pattern_detector",
    "escalation",
    "history",
]

import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())
