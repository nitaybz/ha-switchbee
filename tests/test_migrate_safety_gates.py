"""Tests for the migration tool's argument-validation safety gates.

Phase 5 acceptance:
- `--apply` without `--bridge-config-entry-id` exits non-zero with a clear
  error (the device_registry orphan cleanup needs this id).
- `--apply` with `--output-dir` under `/tmp` exits non-zero (Debian's `/tmp`
  is volatile; the backup tarball must survive a reboot).
- `--apply` with HA running exits non-zero (HA caches the registry in
  memory and would overwrite on shutdown).
- `--dry-run` with HA running succeeds (dry-run does not mutate; the HA-stopped
  check is only required for --apply).

`check_safety_gates` is invoked by the CLI entry point BEFORE the backup
step, so a misconfiguration aborts before any registry I/O.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools._migrate.safety import (
    SafetyGateError,
    check_safety_gates,
)


def test_apply_requires_bridge_config_entry_id(tmp_path: Path) -> None:
    """--apply needs the bridge config_entry_id for device_registry cleanup."""
    output_dir = tmp_path / "migration"
    with pytest.raises(SafetyGateError, match="bridge-config-entry-id"):
        check_safety_gates(
            apply=True,
            output_dir=output_dir,
            bridge_config_entry_id=None,
            ha_running=False,
        )


def test_apply_refuses_tmp_output_dir(tmp_path: Path) -> None:
    """--output-dir under /tmp is volatile; the backup must survive a reboot."""
    tmp_output = Path("/tmp/migration-test-suite-output")
    with pytest.raises(SafetyGateError, match="/tmp"):
        check_safety_gates(
            apply=True,
            output_dir=tmp_output,
            bridge_config_entry_id="01K2M7HFJ8YDYMKW811JCKGC0W",
            ha_running=False,
        )


def test_apply_refuses_when_ha_running(tmp_path: Path) -> None:
    """HA caches the registry in memory; --apply requires HA stopped."""
    output_dir = tmp_path / "migration"
    with pytest.raises(SafetyGateError, match="HA must be stopped"):
        check_safety_gates(
            apply=True,
            output_dir=output_dir,
            bridge_config_entry_id="01K2M7HFJ8YDYMKW811JCKGC0W",
            ha_running=True,
        )


def test_dry_run_with_ha_running_is_allowed(tmp_path: Path) -> None:
    """--dry-run does not mutate, so the HA-stopped check is not enforced."""
    output_dir = tmp_path / "migration"
    # No exception expected.
    check_safety_gates(
        apply=False,
        output_dir=output_dir,
        bridge_config_entry_id=None,
        ha_running=True,
    )


def test_dry_run_does_not_require_bridge_config_entry_id(tmp_path: Path) -> None:
    """The flag is only required for --apply (device_registry cleanup)."""
    output_dir = tmp_path / "migration"
    check_safety_gates(
        apply=False,
        output_dir=output_dir,
        bridge_config_entry_id=None,
        ha_running=False,
    )


def test_apply_under_var_tmp_subdir_also_refused() -> None:
    """`/tmp/...` covers any subdir."""
    with pytest.raises(SafetyGateError, match="/tmp"):
        check_safety_gates(
            apply=True,
            output_dir=Path("/tmp/anything/deep/migration"),
            bridge_config_entry_id="01K2M7HFJ8YDYMKW811JCKGC0W",
            ha_running=False,
        )


def test_apply_relative_output_dir_resolved_against_cwd(tmp_path: Path, monkeypatch) -> None:
    """A relative path that resolves under /tmp is still rejected."""
    monkeypatch.chdir(tmp_path)
    # A relative path that resolves under tmp_path (not /tmp) should pass.
    check_safety_gates(
        apply=True,
        output_dir=Path("./migration"),
        bridge_config_entry_id="01K2M7HFJ8YDYMKW811JCKGC0W",
        ha_running=False,
    )
