"""Helper modules for the ha-switchbee migration CLI.

Each submodule has a narrow purpose:

- `registry_reader`: load `core.entity_registry`, `core.device_registry`,
  `core.area_registry` JSON files and filter homekit_controller rows.
- `homebridge_reader`: scan a node-persist directory and pull the file
  whose `key == "switchbee-configuration"`.
- `mapper`: map every homekit_controller SwitchBee entity to a proposed
  ha-switchbee entry with a confidence tier.
- `applier`: rewrite `core.entity_registry` per the write-list invariant.
- `device_registry_cleaner`: delete orphan device_registry rows whose only
  config_entries reference is the SwitchBee bridge.
- `backup`: tar the four registry files plus the homebridge config.json.
- `safety`: argument-validation safety gates (refuse `/tmp`, refuse with HA
  running, require `--bridge-config-entry-id` on `--apply`).
"""
