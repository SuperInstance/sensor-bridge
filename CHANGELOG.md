# Changelog

All notable changes to **sensor-bridge** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] — 2026-08-04

### Added
- MQTT sensor integration for the exocortex
- **mqtt_client.py**: subscribes to ESP32 sensor topics, publishes config updates
- **normalizer.py**: converts raw sensor readings to standard SensorReading format
- **pattern_detector.py**: detects anomalies — spikes, drift, stuck values, threshold crossings
- **escalation.py**: 4-level escalation protocol (Normal → Warning → Alert → Critical)
- **history.py**: time-series storage with compaction and retention
- **bridge.py**: orchestrator wiring all components together
- **config.yaml**: device definitions, sensor mappings, thresholds
- Two-agent architecture: Ensign (runtime, ESP32) + LaForge (repo agent)
- Full test suite: bridge, history, pattern_detector, escalation, normalizer, mqtt_client
- MIT License
