"""Mapping algorithm for the ha-switchbee migration tool.

Phase 5 / Mapping Algorithm. The mapper consumes:

- a list of HA `homekit_controller` entity-registry rows whose unique_id
  begins with the SwitchBee bridge MAC
- the HA `device_registry` rows (used to extract the SerialNumber that
  homebridge-switchbee bakes into each HomeKit accessory at pairing time:
  `device.hw + '_ID' + device.id` per `homebridge-switchbee/SwitchBee/unified.js:48`).
  This is the PRIMARY mapping path. It is name-independent, rename-proof,
  and matches the structural identity homebridge-switchbee assigns at
  pairing.
- the flat homebridge `switchbee-configuration` cache
  `{item_id: {id, name, hw, type, zone}, ...}`, used only to look up the
  SwitchBee `type` field given an item_id (so we can decide whether the
  item is in v1 scope or a keep_homekit type like SENSOR / TWO_WAY).
- the HA `area_registry` (used only for the legacy name-match tie-breaker
  fallback path).

and produces a `MappingRow` per HA entity describing the proposed action
(`migrate`, `delete`, `keep_homekit`) with a confidence tier (`high`,
`medium`, `low`).

PRIMARY algorithm (Decision #9, revised after live verification on the
STE Smart Home CU): for each HA entity, resolve its `device_id` to a
device_registry row, extract `serial_number` (e.g. `REGULAR_SWITCH_ID152`,
`SOMFY_ID471`, `DIMMABLE_SWITCH_ID1`), parse the trailing `_ID<n>` to
recover the SwitchBee `item.id`. The new unique_id is `{cu_mac}_{item_id}`.

FALLBACK (when the device_registry has no usable SerialNumber for an
entity): name-match the entity's `original_name` against the CU's
`name + " " + zone` index. This is the older mapping path retained for
robustness when SerialNumber is missing.

Both `_aid_iid` (2-part) and `_aid_sid_iid` (3-part) homekit unique_id
shapes are accepted.

This module is pure Python with no `homeassistant.*` import so it can be
unit-tested in a plain Python 3.12 venv.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

_LOGGER = logging.getLogger(__name__)

# Skipped SwitchBee item types: SENSOR + TWO_WAY per Decision #4 / #14, and
# the v1.1 climate/remote types whose HA platforms are not in scope yet.
_KEEP_HOMEKIT_TYPES: frozenset[str] = frozenset(
    {"SENSOR", "TWO_WAY", "THERMOSTAT", "VRF_AC", "IR_DEVICE"}
)

# Whitespace runs (one or more whitespace chars).
_WS_RE = re.compile(r"\s+")

# SerialNumber suffix as set by homebridge-switchbee/SwitchBee/unified.js:48
# (`serial: device.hw + '_ID' + device.id`). The hardware-model prefix
# varies (`REGULAR_SWITCH`, `DIMMABLE_SWITCH`, `SOMFY`, etc) but the
# `_ID<digits>` suffix is invariant.
_SN_ID_RE = re.compile(r"_ID(\d+)$")

Confidence = Literal["high", "medium", "low"]
Action = Literal["migrate", "delete", "keep_homekit"]


def normalize_name(s: str) -> str:
    """Canonical name index key.

    Steps: collapse internal whitespace runs to a single space, strip
    leading/trailing whitespace, casefold. Idempotent.
    """
    if s is None:
        return ""
    collapsed = _WS_RE.sub(" ", s)
    return collapsed.strip().casefold()


@dataclass(frozen=True)
class MappingRow:
    """One row of the migration mapping report.

    `entity_id` is the HA entity_id that will be preserved on adoption.
    `old_unique_id` is the existing homekit_controller unique_id.
    `new_unique_id` is the proposed `{cu_mac}_{item_id}` (None for delete /
    keep_homekit).
    `confidence` is `high` | `medium` | `low`; only `high` and `medium`
    rows with `action == "migrate"` are written by `--apply`.
    `action` is `migrate` | `delete` | `keep_homekit`.
    `reason` is a short human-readable explanation surfaced in report.md.
    """

    entity_id: str
    old_unique_id: str
    new_unique_id: str | None
    confidence: Confidence
    action: Action
    reason: str
    item_id: int | None = None
    sb_type: str | None = None


def _parse_homekit_unique_id(unique_id: str, bridge_mac: str) -> tuple[int, ...] | None:
    """Parse `bridge_mac_aid_iid` or `bridge_mac_aid_sid_iid` into (aid, ...).

    Returns None if the unique_id does not start with `bridge_mac_` or the
    trailing segments are not all integers.
    """
    prefix = f"{bridge_mac}_"
    if not unique_id.startswith(prefix):
        return None
    tail = unique_id[len(prefix) :]
    parts = tail.split("_")
    if not (2 <= len(parts) <= 3):
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _build_cu_name_index(
    cu_devices: Mapping[int | str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build {normalized_full_name: [device, ...]} from the CU device dict.

    `cu_devices` is the flat map `{item_id: {id, name, hw, type, zone}, ...}`
    as produced by homebridge-switchbee's `getDevices()` and persisted under
    the `switchbee-configuration` node-persist key.

    Returns a list of candidates per normalized name so the mapper can apply
    the tie-breaker when multiple items share a normalized name.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for value in cu_devices.values():
        if not isinstance(value, Mapping):
            continue
        name = str(value.get("name", ""))
        zone = str(value.get("zone", ""))
        full = f"{name} {zone}"
        key = normalize_name(full)
        if not key:
            continue
        index.setdefault(key, []).append(dict(value))
    return index


def _resolve_area_name(
    area_id: str | None,
    area_registry: Mapping[str, str] | None,
) -> str | None:
    """Resolve an HA entity's area_id to its area name (lowercased).

    `area_registry` is `{area_id: area_name}`. Returns None if either
    `area_id` or the registry is missing.
    """
    if not area_id or not area_registry:
        return None
    name = area_registry.get(area_id)
    if name is None:
        return None
    return normalize_name(name)


def _build_device_serial_index(
    devices: Iterable[Mapping[str, Any]] | None,
) -> dict[str, int]:
    """Build `{device_id: item_id}` from HA device_registry rows.

    Parses each row's `serial_number` field with the `_ID<digits>$` regex.
    Devices whose serial does not match (the bridge itself, third-party
    devices) are omitted, so callers see `None` on lookup.
    """
    out: dict[str, int] = {}
    if not devices:
        return out
    for d in devices:
        dev_id = d.get("id")
        sn = d.get("serial_number")
        if not dev_id or not sn:
            continue
        m = _SN_ID_RE.search(str(sn))
        if m:
            try:
                out[str(dev_id)] = int(m.group(1))
            except ValueError:  # pragma: no cover
                continue
    return out


def map_entities(
    entities: Iterable[Mapping[str, Any]],
    *,
    cu_devices: Mapping[int | str, Mapping[str, Any]],
    cu_mac: str,
    bridge_mac: str,
    area_registry: Mapping[str, str] | None = None,
    ha_devices: Iterable[Mapping[str, Any]] | None = None,
) -> list[MappingRow]:
    """Map every homekit_controller SwitchBee entity to a proposed action.

    Args:
        entities: an iterable of HA entity-registry rows (dicts). Caller is
            responsible for filtering by `platform == "homekit_controller"`
            and `unique_id.startswith(bridge_mac + "_")`. The mapper applies
            the same checks defensively.
        cu_devices: flat homebridge `switchbee-configuration` map. Used to
            look up the SwitchBee `type` field given an item_id (so we
            can decide v1 scope vs keep_homekit types like SENSOR / TWO_WAY).
        cu_mac: normalized 12-hex CU MAC (lowercase, no separators).
        bridge_mac: the homekit_controller bridge MAC prefix (with the same
            separator style as the source unique_ids; uppercase with colons
            on Moshe's device).
        area_registry: optional `{area_id: area_name}` map for the
            zone-vs-area tie-breaker in the name-match fallback path.
        ha_devices: optional iterable of HA device_registry rows. When
            provided, the mapper uses the PRIMARY SerialNumber-based path
            (resolves entity.device_id -> serial_number -> _ID<n> -> item.id).
            When omitted, the mapper falls back to the name-match path.

    Returns:
        One `MappingRow` per input entity.
    """
    index = _build_cu_name_index(cu_devices)
    device_serial_index = _build_device_serial_index(ha_devices)
    # Normalize cu_devices key to int so SN-resolved item_ids can look up
    # the type field regardless of whether the JSON wrote ids as strings.
    cu_by_int_id: dict[int, Mapping[str, Any]] = {}
    for k, v in cu_devices.items():
        try:
            cu_by_int_id[int(k)] = v
        except (TypeError, ValueError):  # pragma: no cover
            continue

    rows: list[MappingRow] = []
    for raw in entities:
        entity_id = str(raw.get("entity_id", ""))
        unique_id = str(raw.get("unique_id", ""))
        platform = str(raw.get("platform", ""))
        if platform != "homekit_controller":
            continue
        parsed = _parse_homekit_unique_id(unique_id, bridge_mac)
        if parsed is None:
            continue

        # Rule 9: button.*_identify entries are always delete.
        if entity_id.startswith("button.") and entity_id.endswith("_identify"):
            rows.append(
                MappingRow(
                    entity_id=entity_id,
                    old_unique_id=unique_id,
                    new_unique_id=None,
                    confidence="high",
                    action="delete",
                    reason="button.*_identify (no SwitchBee analog)",
                )
            )
            continue

        # PRIMARY: SerialNumber-based lookup.
        device_id = raw.get("device_id")
        if device_id and device_serial_index:
            item_id = device_serial_index.get(str(device_id))
            if item_id is not None:
                # Found via SerialNumber. Look up type from CU map for the
                # keep_homekit gate. If the CU doesn't know about this id
                # (stale homebridge persist), we still migrate but flag it.
                candidate = cu_by_int_id.get(item_id) or {"id": item_id}
                rows.append(
                    _row_for_candidate(
                        entity_id=entity_id,
                        unique_id=unique_id,
                        candidate=candidate,
                        cu_mac=cu_mac,
                        confidence="high",
                        reason="HomeKit SerialNumber -> item.id",
                    )
                )
                continue

        # FALLBACK: name match against the CU index (legacy path).
        original_name = str(raw.get("original_name", "") or "")
        key = normalize_name(original_name)
        candidates = index.get(key, [])

        if len(candidates) == 1:
            row = _row_for_candidate(
                entity_id=entity_id,
                unique_id=unique_id,
                candidate=candidates[0],
                cu_mac=cu_mac,
                confidence="high",
                reason="exact match on (name + zone)",
            )
            rows.append(row)
            continue

        if len(candidates) > 1:
            # Tie-breaker: candidate whose zone matches the entity's
            # area_id-resolved area name.
            area_name = _resolve_area_name(raw.get("area_id"), area_registry)
            tied: list[dict[str, Any]] = []
            if area_name:
                for cand in candidates:
                    if normalize_name(str(cand.get("zone", ""))) == area_name:
                        tied.append(cand)
            if len(tied) == 1:
                rows.append(
                    _row_for_candidate(
                        entity_id=entity_id,
                        unique_id=unique_id,
                        candidate=tied[0],
                        cu_mac=cu_mac,
                        confidence="medium",
                        reason="tie-breaker on area_id->zone",
                    )
                )
                continue
            rows.append(
                MappingRow(
                    entity_id=entity_id,
                    old_unique_id=unique_id,
                    new_unique_id=None,
                    confidence="low",
                    action="keep_homekit",
                    reason="ambiguous name; tie-breaker did not disambiguate",
                )
            )
            continue

        # No candidates by SerialNumber or name. Mark as keep_homekit / low.
        rows.append(
            MappingRow(
                entity_id=entity_id,
                old_unique_id=unique_id,
                new_unique_id=None,
                confidence="low",
                action="keep_homekit",
                reason="no CU device matched (SerialNumber and name)",
            )
        )
    return rows


def _row_for_candidate(
    *,
    entity_id: str,
    unique_id: str,
    candidate: Mapping[str, Any],
    cu_mac: str,
    confidence: Confidence,
    reason: str,
) -> MappingRow:
    """Build a MappingRow from a matched CU device candidate.

    Honors the SENSOR / TWO_WAY / climate keep_homekit list (action becomes
    `keep_homekit` even on an otherwise-high-confidence match, because the
    integration cannot represent these item types in v1).
    """
    item_id_raw = candidate.get("id")
    try:
        item_id = int(item_id_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MappingRow(
            entity_id=entity_id,
            old_unique_id=unique_id,
            new_unique_id=None,
            confidence="low",
            action="keep_homekit",
            reason="CU device matched but item id is not an int",
        )
    sb_type = str(candidate.get("type", ""))
    if sb_type in _KEEP_HOMEKIT_TYPES:
        return MappingRow(
            entity_id=entity_id,
            old_unique_id=unique_id,
            new_unique_id=None,
            confidence=confidence,
            action="keep_homekit",
            reason=f"SwitchBee type {sb_type} not in v1 scope",
            item_id=item_id,
            sb_type=sb_type,
        )
    return MappingRow(
        entity_id=entity_id,
        old_unique_id=unique_id,
        new_unique_id=f"{cu_mac}_{item_id}",
        confidence=confidence,
        action="migrate",
        reason=reason,
        item_id=item_id,
        sb_type=sb_type,
    )


__all__ = ["MappingRow", "map_entities", "normalize_name"]
