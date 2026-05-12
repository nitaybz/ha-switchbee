# SwitchBee Local

Home Assistant integration for the SwitchBee Central Unit, talking to the CU directly over its local WebSocket protocol on port `7891`. No cloud, no homebridge bridge, no HomeKit pairing dance.

- Local-only operation; the integration speaks WebSocket to the CU on the same LAN.
- Push state updates via the CU's `CONFIGURATION_CHANGE` notifications, reflected in HA within ~1 second.
- Native HA platforms: `switch`, `light` (dimmers), `cover` (shutters and Somfy), and `scene`.
- Each SwitchBee item is its own HA device, grouped under one SwitchBee Central Unit hub device.

See the [README](https://github.com/nitaybz/ha-switchbee) for setup, the cutover runbook for migrating from `homebridge-switchbee`, and the source code for the WebSocket protocol details.
