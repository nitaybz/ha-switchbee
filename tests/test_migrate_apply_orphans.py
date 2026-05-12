"""Tests for P7 device_registry orphan cleanup.

Algorithm (Phase 5, Step 3):
1. Load `core.device_registry`.
2. A device row whose `config_entries == [<bridge_config_entry_id>]`
   (exactly one entry, exactly the SwitchBee bridge) is an orphan after the
   78 `button.*_identify` entries are deleted. Remove it.
3. A device row whose `config_entries` contains the SwitchBee bridge AND
   any other integration id is left alone.
4. A device row that does not reference the SwitchBee bridge at all is
   left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools._migrate.device_registry_cleaner import clean_orphan_devices

BRIDGE_CONFIG_ENTRY_ID = "01K2M7HFJ8YDYMKW811JCKGC0W"


def _seed_device_registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 6,
                "key": "core.device_registry",
                "data": {
                    "devices": [
                        {
                            "id": "dev-orphan",
                            "config_entries": [BRIDGE_CONFIG_ENTRY_ID],
                            "name": "SwitchBee Accessory 75",
                            "identifiers": [["homekit_controller", "abc-75"]],
                            "extra_field": "preserve",
                        },
                        {
                            "id": "dev-multi",
                            "config_entries": [BRIDGE_CONFIG_ENTRY_ID, "other-integration-id"],
                            "name": "Shared device",
                            "identifiers": [["homekit_controller", "abc-99"]],
                        },
                        {
                            "id": "dev-other-bridge",
                            "config_entries": ["unrelated-bridge-id"],
                            "name": "Hue Bridge",
                            "identifiers": [["homekit_controller", "hue-1"]],
                        },
                    ],
                    "deleted_devices": [],
                },
            }
        )
    )


def test_orphan_device_rows_deleted(tmp_path: Path) -> None:
    """A device whose ONLY config_entries reference is the SwitchBee bridge
    is removed."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.device_registry"
    _seed_device_registry(registry_path)

    summary = clean_orphan_devices(
        registry_path, bridge_config_entry_id=BRIDGE_CONFIG_ENTRY_ID
    )

    dr = json.loads(registry_path.read_text())
    ids = {d["id"] for d in dr["data"]["devices"]}
    assert "dev-orphan" not in ids
    assert summary.deleted == 1


def test_multi_integration_device_not_deleted(tmp_path: Path) -> None:
    """A device whose config_entries includes the bridge AND another
    integration is left alone."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.device_registry"
    _seed_device_registry(registry_path)

    clean_orphan_devices(
        registry_path, bridge_config_entry_id=BRIDGE_CONFIG_ENTRY_ID
    )

    dr = json.loads(registry_path.read_text())
    by_id = {d["id"]: d for d in dr["data"]["devices"]}
    assert "dev-multi" in by_id
    # The multi-integration row passes through verbatim.
    assert by_id["dev-multi"]["config_entries"] == [
        BRIDGE_CONFIG_ENTRY_ID,
        "other-integration-id",
    ]


def test_unrelated_bridge_device_not_deleted(tmp_path: Path) -> None:
    """A device that does not reference the SwitchBee bridge at all is
    left alone (e.g. Hue bridge accessories)."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.device_registry"
    _seed_device_registry(registry_path)

    clean_orphan_devices(
        registry_path, bridge_config_entry_id=BRIDGE_CONFIG_ENTRY_ID
    )

    dr = json.loads(registry_path.read_text())
    ids = {d["id"] for d in dr["data"]["devices"]}
    assert "dev-other-bridge" in ids


def test_top_level_structure_preserved(tmp_path: Path) -> None:
    """version / minor_version / key / deleted_devices preserved verbatim."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.device_registry"
    _seed_device_registry(registry_path)

    clean_orphan_devices(
        registry_path, bridge_config_entry_id=BRIDGE_CONFIG_ENTRY_ID
    )

    dr = json.loads(registry_path.read_text())
    assert dr["version"] == 1
    assert dr["minor_version"] == 6
    assert dr["key"] == "core.device_registry"
    assert "deleted_devices" in dr["data"]


def test_clean_orphan_devices_atomic_rename(tmp_path: Path) -> None:
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry_path = storage / "core.device_registry"
    _seed_device_registry(registry_path)

    clean_orphan_devices(
        registry_path, bridge_config_entry_id=BRIDGE_CONFIG_ENTRY_ID
    )

    leftovers = list(storage.glob("core.device_registry.tmp*"))
    assert leftovers == []
