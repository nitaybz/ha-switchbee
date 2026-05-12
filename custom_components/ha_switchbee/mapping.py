"""SwitchBee `type` string -> Home Assistant platform mapping.

This module is the single source of truth for "which HA platform does each
SwitchBee item type land on". It is consumed by:

- Phase 3 coordinator: groups CU items by platform when building entity sets.
- Phase 4 platform modules: each `switch.py`, `light.py`, `cover.py`, `scene.py`
  filters the CU snapshot by `map_type_to_platform(item.type) == "switch"`
  (etc.).
- Phase 5 migration tool: reads `DEFERRED_TYPES` to emit a `keep_homekit`
  row for items the integration intentionally does not cover in v1.

Per plan Decision #4 (SENSOR) and Decision #14 (TWO_WAY), those types
return `None` and appear in `DEFERRED_TYPES` so the migration tool can
flag them as v1.1 follow-up. Per the v1 mapping table in the plan,
THERMOSTAT / VRF_AC / IR_DEVICE also return `None` (HA climate / remote
platforms are explicitly out of v1 scope) but are NOT in `DEFERRED_TYPES`
because there is no committed v1.1 mapping for them.

This module is pure Python with no `homeassistant` import.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

# Canonical mapping: SwitchBee `type` string (verbatim from the JS source) ->
# HA platform string, or `None` if the type is intentionally not exposed in v1.
#
# Source for the type strings:
#   /Users/nitaybz/Projects/homebridge-switchbee/SwitchBee/syncHomeKitCache.js
#   /Users/nitaybz/Projects/homebridge-switchbee/SwitchBee/unified.js
#
# Case-sensitive on purpose: the CU emits these strings uppercase verbatim.
MAPPING_TABLE: Final[Mapping[str, str | None]] = {
    # Switch family -> HA `switch`.
    "SWITCH": "switch",
    "TIMED_SWITCH": "switch",
    "GROUP_SWITCH": "switch",
    "TIMED_POWER": "switch",
    "LOCK_GROUP": "switch",
    # Dimmer -> HA `light`.
    "DIMMER": "light",
    # Cover family -> HA `cover`.
    "SHUTTER": "cover",
    "LOUVERED_SHUTTER": "cover",
    "SOMFY": "cover",
    # Scenes -> HA `scene`.
    "SCENARIO": "scene",
    "ROLLING_SCENARIO": "scene",
    # v1.1: climate / remote not in v1 scope.
    "THERMOSTAT": None,
    "VRF_AC": None,
    "IR_DEVICE": None,
    # v1.1: deferred per Decision #4 (SENSOR) and Decision #14 (TWO_WAY).
    "SENSOR": None,
    "TWO_WAY": None,
}

# Types the migration tool should flag as "keep_homekit" / v1.1 follow-up.
# Per plan Decision #14, TWO_WAY is the gap-identified type that the source
# JS plugin has no case for; per Decision #4 SENSOR is the push-only type
# reserved for `binary_sensor` in v1.1.
DEFERRED_TYPES: Final[frozenset[str]] = frozenset({"SENSOR", "TWO_WAY"})


def map_type_to_platform(sb_type: str) -> str | None:
    """Return the HA platform string for a SwitchBee item type.

    Returns `None` if the type is intentionally skipped in v1 OR if the
    type is unknown. This function MUST NOT raise: an unknown / future
    type must not crash entity setup (per Phase 1 contract note in the plan).
    """
    return MAPPING_TABLE.get(sb_type)


__all__ = ["DEFERRED_TYPES", "MAPPING_TABLE", "map_type_to_platform"]
