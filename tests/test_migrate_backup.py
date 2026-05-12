"""Tests for the backup-first invariant (P1).

`create_backup` must produce a `.tar.gz` containing all four `core.*` JSON
registry files PLUS `/var/lib/homebridge/config.json`. The tarball is
written atomically (`.tmp` + fsync + rename) per Phase 5 Step 1.

These tests run against a synthetic temp tree; the real registries are not
needed.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from tools._migrate.backup import BackupInputError, create_backup


def _seed_storage(storage_dir: Path) -> None:
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "core.entity_registry").write_text(
        '{"version": 1, "minor_version": 22, "data": {"entities": []}}'
    )
    (storage_dir / "core.device_registry").write_text(
        '{"version": 1, "minor_version": 6, "data": {"devices": []}}'
    )
    (storage_dir / "core.area_registry").write_text(
        '{"version": 1, "minor_version": 7, "data": {"areas": []}}'
    )
    (storage_dir / "core.config_entries").write_text(
        '{"version": 1, "minor_version": 1, "data": {"entries": []}}'
    )


def _seed_homebridge_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"bridge": {}, "platforms": []}')


def test_create_backup_includes_all_four_registries_and_homebridge(tmp_path: Path) -> None:
    """The tarball must contain entity, device, area, config_entries, and
    the homebridge config.json."""
    storage = tmp_path / ".storage"
    _seed_storage(storage)
    homebridge_cfg = tmp_path / "homebridge" / "config.json"
    _seed_homebridge_config(homebridge_cfg)
    output_dir = tmp_path / "migration"
    output_dir.mkdir()

    tarball = create_backup(
        ha_storage=storage,
        homebridge_config_path=homebridge_cfg,
        output_dir=output_dir,
    )

    assert tarball.exists()
    assert tarball.suffix == ".gz"
    assert tarball.name == "backup.tar.gz"
    with tarfile.open(tarball, "r:gz") as tf:
        names = {Path(n).name for n in tf.getnames()}
    assert names >= {
        "core.entity_registry",
        "core.device_registry",
        "core.area_registry",
        "core.config_entries",
        "config.json",
    }


def test_create_backup_uses_atomic_rename(tmp_path: Path) -> None:
    """No `.tmp` lying around after a successful run."""
    storage = tmp_path / ".storage"
    _seed_storage(storage)
    homebridge_cfg = tmp_path / "homebridge" / "config.json"
    _seed_homebridge_config(homebridge_cfg)
    output_dir = tmp_path / "migration"
    output_dir.mkdir()

    create_backup(
        ha_storage=storage,
        homebridge_config_path=homebridge_cfg,
        output_dir=output_dir,
    )

    leftovers = list(output_dir.glob("backup.tar.gz.tmp*"))
    assert leftovers == []


def test_create_backup_refuses_missing_registry_file(tmp_path: Path) -> None:
    """If a required registry file is missing the backup must refuse."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    # only entity_registry, missing the other three
    (storage / "core.entity_registry").write_text(
        '{"version": 1, "minor_version": 22, "data": {"entities": []}}'
    )
    homebridge_cfg = tmp_path / "homebridge" / "config.json"
    _seed_homebridge_config(homebridge_cfg)
    output_dir = tmp_path / "migration"
    output_dir.mkdir()

    with pytest.raises(BackupInputError, match="core.device_registry"):
        create_backup(
            ha_storage=storage,
            homebridge_config_path=homebridge_cfg,
            output_dir=output_dir,
        )


def test_create_backup_refuses_missing_homebridge_config(tmp_path: Path) -> None:
    """The homebridge config.json snapshot is part of the rollback artifact."""
    storage = tmp_path / ".storage"
    _seed_storage(storage)
    homebridge_cfg = tmp_path / "homebridge" / "config.json"
    # Intentionally not created.
    output_dir = tmp_path / "migration"
    output_dir.mkdir()

    with pytest.raises(BackupInputError, match="config.json"):
        create_backup(
            ha_storage=storage,
            homebridge_config_path=homebridge_cfg,
            output_dir=output_dir,
        )


def test_create_backup_creates_output_dir_when_missing(tmp_path: Path) -> None:
    """Operator typically passes a fresh `migration-{timestamp}` dir."""
    storage = tmp_path / ".storage"
    _seed_storage(storage)
    homebridge_cfg = tmp_path / "homebridge" / "config.json"
    _seed_homebridge_config(homebridge_cfg)
    output_dir = tmp_path / "fresh_dir_that_does_not_exist"
    assert not output_dir.exists()

    tarball = create_backup(
        ha_storage=storage,
        homebridge_config_path=homebridge_cfg,
        output_dir=output_dir,
    )

    assert tarball.exists()
    assert output_dir.is_dir()
