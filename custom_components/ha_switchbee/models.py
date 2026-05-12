"""Typed device records and value codecs for SwitchBee items.

This module is intentionally pure Python with no `homeassistant` import, so it
is importable in any environment (CI, unit tests, ad-hoc scripts).

Scope (Phase 2):
- `SwitchBeeDevice`: a frozen, slotted record for the CU item shape
  `{id, name, hw, type, zone, state}` returned by `GET_CONFIGURATION`.
- Value codecs that round-trip raw CU values to typed Python values:
    * `decode_on_off` / `encode_on_off` for ON_OFF items
      (SWITCH / TIMED_SWITCH / GROUP_SWITCH / LOCK_GROUP / TIMED_POWER /
      SCENARIO / ROLLING_SCENARIO).
    * `decode_dimmer` / `encode_dimmer` for DIMMER (0-100, clamped).
    * `decode_shutter` / `encode_shutter` for SHUTTER / LOUVERED_SHUTTER
      (0-100 position, clamped; optional tilt accepted on decode).
    * `encode_somfy` for SOMFY (command set: UP / DOWN / STOP).

Offline / unavailable semantics (raw values `-1` and `"OFFLINE"`) belong on
the platform layer in later phases; this module deals with the in-range
domain only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

# Public set of valid SOMFY commands. Source: SwitchBee CU accepts these three
# verbs in the OPERATE.value field for SOMFY items.
SOMFY_COMMANDS: Final[frozenset[str]] = frozenset({"UP", "DOWN", "STOP"})


@dataclass(frozen=True, slots=True)
class SwitchBeeDevice:
    """One item from the CU `GET_CONFIGURATION` response.

    Mirrors the JS source's `device` object (see
    `/Users/nitaybz/Projects/homebridge-switchbee/SwitchBee/unified.js`
    `deviceInformation`): the canonical keys are `id`, `name`, `hw`, `type`,
    `zone`, and the current `state` value (shape varies by type).
    """

    id: int
    name: str
    hw: str
    type: str
    zone: str
    state: Any = None

    @classmethod
    def from_cu_item(cls, item: Mapping[str, Any]) -> SwitchBeeDevice:
        """Build a record from a raw CU item dict.

        Unknown keys are ignored. Missing optional keys default to None / "".
        """
        return cls(
            id=int(item["id"]),
            name=str(item.get("name", "")),
            hw=str(item.get("hw", "")),
            type=str(item.get("type", "")),
            zone=str(item.get("zone", "")),
            state=item.get("state"),
        )


# ---------------------------------------------------------------------------
# ON_OFF codec
# ---------------------------------------------------------------------------
def decode_on_off(raw: str) -> bool:
    """Decode a SwitchBee ON_OFF string to a bool.

    'ON' -> True. Anything else (including 'OFF', '-1', 'OFFLINE') -> False.
    Treating non-'ON' as False keeps the codec total; the platform layer is
    responsible for distinguishing 'OFF' (known-off) from 'OFFLINE' / -1
    (unavailable) when surfacing entity availability.
    """
    return raw == "ON"


def encode_on_off(value: bool) -> str:
    """Encode a Python bool to the wire string 'ON' / 'OFF'."""
    return "ON" if value else "OFF"


# ---------------------------------------------------------------------------
# DIMMER codec
# ---------------------------------------------------------------------------
def decode_dimmer(raw: int) -> int:
    """Decode a SwitchBee DIMMER value to a 0-100 brightness int.

    The CU sends an int in [0, 100] for normal operation. Out-of-range
    sentinels (-1, 'OFFLINE') are the platform layer's problem and are not
    handled here; the contract is "given an in-range int, return it".
    """
    return int(raw)


def encode_dimmer(value: int) -> int:
    """Encode a brightness int for the wire, clamped to [0, 100]."""
    if value < 0:
        return 0
    if value > 100:
        return 100
    return int(value)


# ---------------------------------------------------------------------------
# SHUTTER codec (also used for LOUVERED_SHUTTER position)
# ---------------------------------------------------------------------------
def decode_shutter(raw: int, tilt: int | None = None) -> int:
    """Decode a SwitchBee SHUTTER position to a 0-100 int.

    `tilt` is accepted for LOUVERED_SHUTTER forward-compatibility but is not
    used in v1 (tilt control is deferred per Phase 2 plan note).
    """
    del tilt  # reserved for v2 louvered tilt
    return int(raw)


def encode_shutter(value: int) -> int:
    """Encode a target shutter position for the wire, clamped to [0, 100]."""
    if value < 0:
        return 0
    if value > 100:
        return 100
    return int(value)


# ---------------------------------------------------------------------------
# SOMFY command codec
# ---------------------------------------------------------------------------
def encode_somfy(command: str) -> str:
    """Validate and return a SOMFY command string.

    Raises ValueError for anything outside `SOMFY_COMMANDS`.
    """
    if command not in SOMFY_COMMANDS:
        raise ValueError(
            f"unknown SOMFY command {command!r}; expected one of {sorted(SOMFY_COMMANDS)}"
        )
    return command


__all__ = [
    "SOMFY_COMMANDS",
    "SwitchBeeDevice",
    "decode_dimmer",
    "decode_on_off",
    "decode_shutter",
    "encode_dimmer",
    "encode_on_off",
    "encode_shutter",
    "encode_somfy",
]
