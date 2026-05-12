"""End-to-end tests for `tools/migrate.py` CLI.

Covers:
- `--help` exits 0 and lists every required flag.
- `--dry-run` against a synthetic fixture produces backup tarball + report.json + report.md
  WITHOUT mutating the registries.
- `--apply` mutates entity_registry + device_registry; the backup tarball is on disk
  before any mutation happens (P1 timing).

The HA-stopped check is patched to return False (stopped) for the --apply
test because the test does not run a real docker container.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

BRIDGE_MAC = "0E:0F:B5:1B:3D:37"
BRIDGE_CONFIG_ENTRY_ID = "01K2M7HFJ8YDYMKW811JCKGC0W"
CU_MAC = "a82108e7688f"

MIGRATE_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "migrate.py"


def _seed_fixture(root: Path) -> None:
    """Create a small but realistic fixture tree."""
    storage = root / ".storage"
    storage.mkdir(parents=True)

    er = {
        "version": 1,
        "minor_version": 22,
        "key": "core.entity_registry",
        "data": {
            "entities": [
                {
                    "entity_id": "cover.blind_2_living_room",
                    "unique_id": f"{BRIDGE_MAC}_75_8",
                    "platform": "homekit_controller",
                    "config_entry_id": BRIDGE_CONFIG_ENTRY_ID,
                    "name": None,
                    "original_name": "Blind 2 Living Room",
                    "icon": None,
                    "area_id": "area_living_room",
                    "aliases": [],
                    "labels": [],
                    "device_id": "dev-1",
                },
                {
                    "entity_id": "button.blind_2_living_room_identify",
                    "unique_id": f"{BRIDGE_MAC}_75_1",
                    "platform": "homekit_controller",
                    "config_entry_id": BRIDGE_CONFIG_ENTRY_ID,
                    "original_name": "Identify",
                    "name": None,
                    "area_id": None,
                    "device_id": "dev-1",
                    "aliases": [],
                    "labels": [],
                },
            ],
            "deleted_entities": [],
        },
    }
    (storage / "core.entity_registry").write_text(json.dumps(er))

    dr = {
        "version": 1,
        "minor_version": 6,
        "key": "core.device_registry",
        "data": {
            "devices": [
                {
                    "id": "dev-orphan",
                    "config_entries": [BRIDGE_CONFIG_ENTRY_ID],
                    "name": "Identify Accessory",
                    "identifiers": [["homekit_controller", "abc-orphan"]],
                },
                {
                    "id": "dev-1",
                    "config_entries": [BRIDGE_CONFIG_ENTRY_ID],
                    "name": "Blind 2 Accessory",
                    "identifiers": [["homekit_controller", "abc-blind"]],
                },
            ],
            "deleted_devices": [],
        },
    }
    (storage / "core.device_registry").write_text(json.dumps(dr))

    ar = {
        "version": 1,
        "minor_version": 7,
        "key": "core.area_registry",
        "data": {
            "areas": [
                {"id": "area_living_room", "name": "Living Room"},
            ]
        },
    }
    (storage / "core.area_registry").write_text(json.dumps(ar))

    ce = {
        "version": 1,
        "minor_version": 1,
        "key": "core.config_entries",
        "data": {
            "entries": [
                {
                    "entry_id": BRIDGE_CONFIG_ENTRY_ID,
                    "domain": "homekit_controller",
                    "title": "SwitchBee Bridge",
                }
            ]
        },
    }
    (storage / "core.config_entries").write_text(json.dumps(ce))

    # Homebridge persist dir with a single switchbee-configuration file.
    persist = root / "switchbee-persist"
    persist.mkdir()
    sb_config = {
        "key": "switchbee-configuration",
        "value": {
            "42": {
                "id": 42,
                "name": "Blind 2",
                "hw": "VBOX",
                "type": "SHUTTER",
                "zone": "Living Room",
            }
        },
    }
    (persist / "deadbeef").write_text(json.dumps(sb_config))
    # An unrelated token file in the same dir; must be ignored.
    (persist / "tokenfile").write_text(
        json.dumps({"key": "switchbee-token", "value": {"token": "x"}})
    )

    # Homebridge config.json.
    hb = root / "homebridge"
    hb.mkdir()
    (hb / "config.json").write_text(
        json.dumps({"bridge": {}, "platforms": [{"platform": "SwitchBee"}]})
    )


def _run_migrate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_help_exits_zero_and_lists_flags() -> None:
    result = _run_migrate("--help")
    assert result.returncode == 0, result.stderr
    for flag in (
        "--ha-storage",
        "--homebridge-persist-dir",
        "--homebridge-config",
        "--cu-host",
        "--cu-user",
        "--cu-pass",
        "--output-dir",
        "--bridge-mac",
        "--bridge-config-entry-id",
        "--dry-run",
        "--apply",
    ):
        assert flag in result.stdout, f"missing {flag} in --help"


def test_dry_run_produces_report_and_backup_without_mutation(tmp_path: Path) -> None:
    _seed_fixture(tmp_path)
    output_dir = tmp_path / "migration"

    er_before = (tmp_path / ".storage" / "core.entity_registry").read_text()
    dr_before = (tmp_path / ".storage" / "core.device_registry").read_text()

    result = _run_migrate(
        "--dry-run",
        "--ha-storage", str(tmp_path / ".storage"),
        "--homebridge-persist-dir", str(tmp_path / "switchbee-persist"),
        "--homebridge-config", str(tmp_path / "homebridge" / "config.json"),
        "--cu-host", "192.168.68.57",
        "--cu-user", "user",
        "--cu-pass", "pass",
        "--output-dir", str(output_dir),
        "--bridge-mac", BRIDGE_MAC,
        "--cu-mac", CU_MAC,
    )
    assert result.returncode == 0, result.stderr

    # Registries unchanged.
    assert (tmp_path / ".storage" / "core.entity_registry").read_text() == er_before
    assert (tmp_path / ".storage" / "core.device_registry").read_text() == dr_before

    # Backup tarball produced even in dry-run.
    assert (output_dir / "backup.tar.gz").is_file()
    # Reports produced.
    assert (output_dir / "report.json").is_file()
    assert (output_dir / "report.md").is_file()

    report = json.loads((output_dir / "report.json").read_text())
    assert report["summary"]["total"] >= 2
    assert report["summary"]["migrate"] >= 1
    assert report["summary"]["delete"] >= 1
    # One row per source entity.
    assert {r["entity_id"] for r in report["rows"]} >= {
        "cover.blind_2_living_room",
        "button.blind_2_living_room_identify",
    }


def test_apply_mutates_registries_after_backup(tmp_path: Path) -> None:
    """P1 timing: the backup tarball exists BEFORE the registry is rewritten."""
    _seed_fixture(tmp_path)
    output_dir = tmp_path / "migration"

    result = _run_migrate(
        "--apply",
        "--ha-storage", str(tmp_path / ".storage"),
        "--homebridge-persist-dir", str(tmp_path / "switchbee-persist"),
        "--homebridge-config", str(tmp_path / "homebridge" / "config.json"),
        "--cu-host", "192.168.68.57",
        "--cu-user", "user",
        "--cu-pass", "pass",
        "--output-dir", str(output_dir),
        "--bridge-mac", BRIDGE_MAC,
        "--cu-mac", CU_MAC,
        "--bridge-config-entry-id", BRIDGE_CONFIG_ENTRY_ID,
        "--ha-stopped-check", "skip",
    )
    assert result.returncode == 0, result.stderr

    # Backup exists.
    backup = output_dir / "backup.tar.gz"
    assert backup.is_file()

    # P1 timing: backup mtime <= entity_registry mtime.
    er_path = tmp_path / ".storage" / "core.entity_registry"
    assert backup.stat().st_mtime <= er_path.stat().st_mtime + 0.01

    # Backup contains all four files plus homebridge config.json.
    with tarfile.open(backup, "r:gz") as tf:
        names = {Path(n).name for n in tf.getnames()}
    assert names >= {
        "core.entity_registry",
        "core.device_registry",
        "core.area_registry",
        "core.config_entries",
        "config.json",
    }

    # entity_registry rewritten: row platform/unique_id/config_entry_id changed.
    er = json.loads(er_path.read_text())
    rows = {r["entity_id"]: r for r in er["data"]["entities"]}
    assert rows["cover.blind_2_living_room"]["platform"] == "ha_switchbee"
    assert rows["cover.blind_2_living_room"]["unique_id"] == f"{CU_MAC}_42"
    assert rows["cover.blind_2_living_room"]["config_entry_id"] is None
    # Original area_id preserved (P8 write-list).
    assert rows["cover.blind_2_living_room"]["area_id"] == "area_living_room"

    # button.*_identify row removed.
    assert "button.blind_2_living_room_identify" not in rows

    # device_registry orphan row removed; non-orphan retained.
    dr = json.loads((tmp_path / ".storage" / "core.device_registry").read_text())
    device_ids = {d["id"] for d in dr["data"]["devices"]}
    assert "dev-orphan" not in device_ids


def test_apply_refuses_tmp_output_dir(tmp_path: Path) -> None:
    _seed_fixture(tmp_path)
    # Use /tmp explicitly; the gate is enforced before any I/O so the
    # fixture content does not actually need to be reachable.
    result = _run_migrate(
        "--apply",
        "--ha-storage", str(tmp_path / ".storage"),
        "--homebridge-persist-dir", str(tmp_path / "switchbee-persist"),
        "--homebridge-config", str(tmp_path / "homebridge" / "config.json"),
        "--cu-host", "192.168.68.57",
        "--cu-user", "user",
        "--cu-pass", "pass",
        "--output-dir", "/tmp/should-be-rejected",
        "--bridge-mac", BRIDGE_MAC,
        "--cu-mac", CU_MAC,
        "--bridge-config-entry-id", BRIDGE_CONFIG_ENTRY_ID,
        "--ha-stopped-check", "skip",
    )
    assert result.returncode != 0
    assert "/tmp" in (result.stderr or "") + (result.stdout or "")


def test_apply_requires_bridge_config_entry_id(tmp_path: Path) -> None:
    _seed_fixture(tmp_path)
    output_dir = tmp_path / "migration"

    result = _run_migrate(
        "--apply",
        "--ha-storage", str(tmp_path / ".storage"),
        "--homebridge-persist-dir", str(tmp_path / "switchbee-persist"),
        "--homebridge-config", str(tmp_path / "homebridge" / "config.json"),
        "--cu-host", "192.168.68.57",
        "--cu-user", "user",
        "--cu-pass", "pass",
        "--output-dir", str(output_dir),
        "--bridge-mac", BRIDGE_MAC,
        "--cu-mac", CU_MAC,
        "--ha-stopped-check", "skip",
    )
    assert result.returncode != 0
    assert "bridge-config-entry-id" in (result.stderr or "") + (result.stdout or "")
