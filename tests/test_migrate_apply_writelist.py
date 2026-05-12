"""Tests for the write-list invariant (P8).

Per Decision #13 the applier mutates ONLY `platform`, `unique_id`, and
`config_entry_id` on each migrated entity-registry row. Every other field
(icon, aliases, labels, area_id, original_name, name, hypothetical future
minor-version fields like `cosmic_field`) must pass through verbatim.

`button.*_identify` rows are removed from the entities array entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools._migrate.applier import EntityRegistryUpdate, apply_entity_registry
from tools._migrate.mapper import MappingRow

BRIDGE_MAC = "0E:0F:B5:1B:3D:37"
CU_MAC = "a82108e7688f"


def _seed_registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 22,
                "key": "core.entity_registry",
                "data": {
                    "entities": [
                        {
                            "entity_id": "cover.blind_2_living_room",
                            "unique_id": f"{BRIDGE_MAC}_75_8",
                            "platform": "homekit_controller",
                            "config_entry_id": "01K2M7HFJ8YDYMKW811JCKGC0W",
                            "name": "Blind 2 LR",
                            "original_name": "Blind 2 Living Room",
                            "icon": "mdi:window-shutter",
                            "area_id": "living_room",
                            "aliases": ["lr-blind"],
                            "labels": ["sb"],
                            "cosmic_field": 42,
                            "device_id": "dev-1",
                        },
                        {
                            "entity_id": "button.blind_2_living_room_identify",
                            "unique_id": f"{BRIDGE_MAC}_75_1",
                            "platform": "homekit_controller",
                            "config_entry_id": "01K2M7HFJ8YDYMKW811JCKGC0W",
                            "name": None,
                            "original_name": "Identify",
                            "icon": None,
                            "area_id": "living_room",
                            "aliases": [],
                            "labels": [],
                            "device_id": "dev-1",
                        },
                        {
                            "entity_id": "light.hue_bulb",
                            "unique_id": "AA:BB:CC:DD:EE:FF_1_1",
                            "platform": "homekit_controller",
                            "config_entry_id": "other-bridge-id",
                            "name": "Hue Bulb",
                            "original_name": "Hue Bulb",
                            "icon": None,
                            "area_id": None,
                            "aliases": [],
                            "labels": [],
                            "device_id": "dev-2",
                        },
                    ],
                    "deleted_entities": [],
                },
                "extra_top_level": {"preserve": "me"},
            }
        )
    )


def test_apply_mutates_only_three_fields(tmp_path: Path) -> None:
    """Migrated row has platform/unique_id/config_entry_id rewritten; every
    other field is byte-identical."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.entity_registry"
    _seed_registry(registry_path)

    update = EntityRegistryUpdate(
        rewrites=[
            MappingRow(
                entity_id="cover.blind_2_living_room",
                old_unique_id=f"{BRIDGE_MAC}_75_8",
                new_unique_id=f"{CU_MAC}_42",
                confidence="high",
                action="migrate",
                reason="exact match",
                item_id=42,
                sb_type="SHUTTER",
            )
        ],
        deletes=[
            MappingRow(
                entity_id="button.blind_2_living_room_identify",
                old_unique_id=f"{BRIDGE_MAC}_75_1",
                new_unique_id=None,
                confidence="high",
                action="delete",
                reason="identify button",
            )
        ],
    )
    summary = apply_entity_registry(registry_path, update)

    er = json.loads(registry_path.read_text())
    rows = er["data"]["entities"]
    by_id = {r["entity_id"]: r for r in rows}

    # Migrated row: platform / unique_id / config_entry_id changed.
    migrated = by_id["cover.blind_2_living_room"]
    assert migrated["platform"] == "ha_switchbee"
    assert migrated["unique_id"] == f"{CU_MAC}_42"
    assert migrated["config_entry_id"] is None
    # Every other field byte-identical to the seed.
    assert migrated["name"] == "Blind 2 LR"
    assert migrated["original_name"] == "Blind 2 Living Room"
    assert migrated["icon"] == "mdi:window-shutter"
    assert migrated["area_id"] == "living_room"
    assert migrated["aliases"] == ["lr-blind"]
    assert migrated["labels"] == ["sb"]
    assert migrated["cosmic_field"] == 42
    assert migrated["device_id"] == "dev-1"

    # Deleted row gone from the entities array.
    assert "button.blind_2_living_room_identify" not in by_id

    # Unrelated row left alone.
    assert by_id["light.hue_bulb"]["platform"] == "homekit_controller"
    assert by_id["light.hue_bulb"]["config_entry_id"] == "other-bridge-id"

    # Top-level structure preserved (version, minor_version, key, extras).
    assert er["version"] == 1
    assert er["minor_version"] == 22
    assert er["key"] == "core.entity_registry"
    assert er["extra_top_level"] == {"preserve": "me"}
    assert "deleted_entities" in er["data"]

    # Summary counts match.
    assert summary.migrated == 1
    assert summary.deleted == 1


def test_apply_idempotent_when_run_twice(tmp_path: Path) -> None:
    """Running --apply twice in a row is safe: the second run is a no-op."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.entity_registry"
    _seed_registry(registry_path)

    update = EntityRegistryUpdate(
        rewrites=[
            MappingRow(
                entity_id="cover.blind_2_living_room",
                old_unique_id=f"{BRIDGE_MAC}_75_8",
                new_unique_id=f"{CU_MAC}_42",
                confidence="high",
                action="migrate",
                reason="exact",
                item_id=42,
                sb_type="SHUTTER",
            )
        ],
        deletes=[],
    )
    apply_entity_registry(registry_path, update)
    first = registry_path.read_text()
    apply_entity_registry(registry_path, update)
    second = registry_path.read_text()
    # The second run finds no homekit_controller row matching the rewrite's
    # entity_id (it is already migrated). Output is byte-identical.
    assert first == second


def test_apply_uses_atomic_rename(tmp_path: Path) -> None:
    """No leftover `.tmp` after a successful apply."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.entity_registry"
    _seed_registry(registry_path)

    update = EntityRegistryUpdate(rewrites=[], deletes=[])
    apply_entity_registry(registry_path, update)

    leftovers = list(storage.glob("core.entity_registry.tmp*"))
    assert leftovers == []
