"""Backup-first invariant (P1) for the migration tool.

`create_backup` tars the four HA registry files plus the running homebridge
config.json into `output_dir/backup.tar.gz`. The tarball is written to
`backup.tar.gz.tmp`, fsynced, then atomic-renamed onto the final name. The
applier MUST call this BEFORE any registry mutation; backup-before-write is
the load-bearing rollback invariant for Phase 5.

This module is HA-free pure Python.
"""

from __future__ import annotations

import logging
import os
import tarfile
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_REQUIRED_REGISTRY_FILES = (
    "core.entity_registry",
    "core.device_registry",
    "core.area_registry",
    "core.config_entries",
)


class BackupInputError(ValueError):
    """A required input file is missing or unreadable."""


def create_backup(
    *,
    ha_storage: Path,
    homebridge_config_path: Path,
    output_dir: Path,
) -> Path:
    """Tar the four HA registry files and the homebridge config.json.

    Args:
        ha_storage: path to HA's `.storage/` directory; must contain the
            four `core.*` JSON files.
        homebridge_config_path: path to the running `/var/lib/homebridge/config.json`
            (or a sanitized fixture).
        output_dir: directory where `backup.tar.gz` will be written; created
            if missing.

    Returns:
        The absolute path to the produced `backup.tar.gz`.

    Raises:
        BackupInputError: if any required input file is missing.
    """
    ha_storage = Path(ha_storage)
    homebridge_config_path = Path(homebridge_config_path)
    output_dir = Path(output_dir)

    for name in _REQUIRED_REGISTRY_FILES:
        path = ha_storage / name
        if not path.is_file():
            raise BackupInputError(
                f"Required HA registry file missing: {name} (looked in {ha_storage})"
            )
    if not homebridge_config_path.is_file():
        raise BackupInputError(
            f"homebridge config.json missing: {homebridge_config_path}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / "backup.tar.gz"
    tmp = output_dir / "backup.tar.gz.tmp"

    # Always write to .tmp first then atomic-rename so a crash mid-write
    # cannot leave a half-written `backup.tar.gz` that masquerades as a
    # valid rollback artifact.
    if tmp.exists():
        tmp.unlink()
    with tarfile.open(tmp, "w:gz") as tf:
        for name in _REQUIRED_REGISTRY_FILES:
            tf.add(ha_storage / name, arcname=name)
        tf.add(homebridge_config_path, arcname="config.json")

    # fsync the tar contents to disk before the rename.
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())

    os.replace(tmp, final)
    _LOGGER.info("backup.tar.gz written to %s", final)
    return final


__all__ = ["BackupInputError", "create_backup"]
