# Sensor Bridge

*Connects real hardware devices to the exocortex.*

```
ESP32 reads sensors → MQTT → sensor_bridge → exocortex
                                ↓
                    normalize → detect patterns → escalate → store
```

## Architecture

The sensor bridge sits between the physical world (ESP32s reading sensors on
the vessel) and the cognitive world (the exocortex memory layer). It is the
sensory nervous system — it receives raw data, normalizes it, detects patterns,
and routes escalations according to the two-agent protocol.

### Two Agents

The system follows the [Two Agents Not One](https://github.com/SuperInstance/engine-ensign)
architecture:

- **Ensign (runtime agent)** — on the ESP32. Runs the fast loop: read sensors,
  check thresholds, display values, publish MQTT. Procedural, deterministic, cheap.
- **LaForge (repo agent)** — lives in the repo. Wakes when paged. Reviews anomalies,
  adjusts thresholds, writes new procedures. Reflective, expensive, episodic.

The sensor bridge is the communication layer between them. It does NOT replace
either agent — it routes information between them.

### Components

| Module | Role |
|--------|------|
| `mqtt_client.py` | Subscribes to ESP32 sensor topics, publishes config updates |
| `normalizer.py` | Converts raw sensor readings to standard `SensorReading` format |
| `pattern_detector.py` | Detects anomalies: spikes, drift, stuck values, threshold crossings |
| `escalation.py` | Routes events through the 4-level escalation protocol |
| `history.py` | Time-series storage with compaction and retention |
| `bridge.py` | Orchestrator — wires everything together |
| `config.yaml` | Device definitions, sensor mappings, thresholds |

### MQTT Topic Structure

```
vessel/{device_id}/sensors/{sensor_name}   — raw sensor readings
vessel/{device_id}/alerts                  — alert events
vessel/{device_id}/status                  — device heartbeat / batch readings
vessel/{device_id}/config                  — config updates from LaForge
```

### Escalation Protocol

| Level | Name | What Happens |
|-------|------|-------------|
| 0 | Normal | Ensign handles. No notification. Data logged. |
| 1 | Warning | Ensign handles. Logged for LaForge's next review. |
| 2 | Alert | Ensign handles. Captain notified immediately. LaForge paged (normal priority). |
| 3 | Critical | All hands notified. LaForge invoked urgently. |

The escalation module applies cooldown logic — it won't repeatedly page LaForge
for the same sensor anomaly every cycle. Once escalated, that sensor enters a
5-minute cooldown before re-escalation.

## Installation

```bash
cd /home/eileen/projects/sensor-bridge
pip install -e ".[dev]"
```

## Usage

### Run the bridge

```bash
python -m sensor_bridge.bridge --config src/sensor_bridge/config.yaml
```

### Programmatic API

```python
from sensor_bridge.bridge import Bridge

bridge = Bridge.from_config("src/sensor_bridge/config.yaml")
bridge.start()

# Inject a reading manually (for testing without an ESP32)
events = bridge.inject_reading("engine_ensign_1", "coolant_temp", 95.0)

# Get status
status = bridge.get_status()
print(status)

bridge.stop()
```

### Run tests

```bash
pytest -v
```

## Configuration

Edit `src/sensor_bridge/config.yaml` to define:

- MQTT broker settings
- Device definitions (sensor types, units, ranges)
- Threshold values (warning/critical per sensor)
- Anomaly detection parameters (spike sensitivity, drift rate, stuck band)
- Escalation routing (who gets notified at each level)
- Storage settings (retention, compaction)

LaForge modifies this file — the ensign reads it. This is the boundary between
the repo agent and the runtime agent.

## Integration

### With engine-ensign (ESP32 firmware)

The engine-ensign ESP32 firmware publishes sensor data over MQTT (or serial).
The sensor bridge subscribes and processes. The ensign handles the fast loop
(read → threshold check → display → publish). The bridge handles the slow loop
(normalize → pattern detect → escalate → store).

### With the exocortex

Sensor readings are stored as time-series data in SQLite. The exocortex's
memory layer can query recent readings, historical stats, and anomaly events
to provide context for Wesley's responses.

## License

MIT — see [LICENSE](LICENSE).
