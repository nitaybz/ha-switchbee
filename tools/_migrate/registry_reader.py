"""Reader helpers for HA's `core.*` storage JSON files.

Loads the four registry files the migration tool touches:

- `core.entity_registry`
- `core.device_registry`
- `core.area_registry`
- `core.config_entries`

and exposes a small filter to pick `homekit_controller` entries whose
unique_id starts with the SwitchBee bridge MAC.

Supported entity_registry shape:
    {"version": 1, "minor_version": <int>, "key": "core.entity_registry",
     "data": {"entities": [...], ...}}

The supported `minor_version` range is `[10, 30]` per the plan (Phase 5
Task 5.2). Out-of-range values raise `UnsupportedRegistryVersionError`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

ENTITY_REGISTRY_MINOR_VERSION_MIN = 10
ENTITY_REGISTRY_MINOR_VERSION_MAX = 30


class UnsupportedRegistryVersionError(RuntimeError):
    """The entity_registry minor_version is outside the supported range."""


def load_entity_registry(path: Path) -> dict[str, Any]:
    """Load `core.entity_registry` and validate the version.

    Raises `UnsupportedRegistryVersionError` if `minor_version` is outside
    `[10, 30]`. Returns the full parsed JSON dict (NOT just the entities
    array) so the applier can re-serialize with the same top-level shape.
    """
    raw = json.loads(Path(path).read_text())
    version = raw.get("version")
    minor = raw.get("minor_version")
    if version != 1:
        raise UnsupportedRegistryVersionError(f"entity_registry version must be 1, got {version!r}")
    if not isinstance(minor, int) or not (
        ENTITY_REGISTRY_MINOR_VERSION_MIN <= minor <= ENTITY_REGISTRY_MINOR_VERSION_MAX
    ):
        raise UnsupportedRegistryVersionError(
            f"entity_registry minor_version {minor!r} outside supported "
            f"range [{ENTITY_REGISTRY_MINOR_VERSION_MIN}, "
            f"{ENTITY_REGISTRY_MINOR_VERSION_MAX}]"
        )
    return raw


def filter_homekit_switchbee(
    entities: Iterable[Mapping[str, Any]],
    *,
    bridge_mac: str,
) -> list[Mapping[str, Any]]:
    """Return only homekit_controller entities whose unique_id begins with
    `{bridge_mac}_`. Accepts both `_aid_iid` and `_aid_sid_iid` shapes."""
    prefix = f"{bridge_mac}_"
    return [
        e
        for e in entities
        if e.get("platform") == "homekit_controller"
        and isinstance(e.get("unique_id"), str)
        and e["unique_id"].startswith(prefix)
    ]


def load_area_registry(path: Path) -> dict[str, str]:
    """Load `core.area_registry` and return a `{area_id: area_name}` dict.

    Returns an empty dict if the file is missing (the tie-breaker is
    optional; the mapper will fall back to keep_homekit/low when areas
    are not available).
    """
    p = Path(path)
    if not p.is_file():
        return {}
    raw = json.loads(p.read_text())
    areas = raw.get("data", {}).get("areas", [])
    return {a["id"]: a.get("name", "") for a in areas if "id" in a}


def load_device_registry(path: Path) -> list[Mapping[str, Any]]:
    """Load `core.device_registry` and return the `devices` array.

    The mapper uses `serial_number` per row to extract the SwitchBee
    item.id from each homebridge-paired accessory's HomeKit metadata
    (`device.hw + "_ID" + device.id`, set by
    `homebridge-switchbee/SwitchBee/unified.js:48`). Returns an empty
    list if the file is missing (mapper falls back to name matching).
    """
    p = Path(path)
    if not p.is_file():
        return []
    raw = json.loads(p.read_text())
    return list(raw.get("data", {}).get("devices", []))


__all__ = [
    "ENTITY_REGISTRY_MINOR_VERSION_MAX",
    "ENTITY_REGISTRY_MINOR_VERSION_MIN",
    "UnsupportedRegistryVersionError",
    "filter_homekit_switchbee",
    "load_area_registry",
    "load_device_registry",
    "load_entity_registry",
]
