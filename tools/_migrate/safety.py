"""Argument-validation safety gates for the migration tool.

Three gates are enforced before the applier does any I/O:

- `--apply` requires `--bridge-config-entry-id` (device_registry cleanup).
- `--apply --output-dir /tmp/...` is refused (volatile filesystem; the
  backup tarball would be wiped on reboot, violating the 30-day retention
  rule).
- `--apply` while HA is running is refused (HA caches the registry in
  memory and would overwrite the file on shutdown).

`--dry-run` is intentionally permissive: it does not mutate the registry
so neither the bridge id nor the HA-stopped check applies.

The CLI is responsible for probing `ha_running` (via `docker compose ps`)
and passing the boolean here; this module stays free of subprocess code so
it can be unit-tested without docker.
"""

from __future__ import annotations

from pathlib import Path


class SafetyGateError(RuntimeError):
    """A safety gate refused the requested operation.

    Carries a human-readable message; CLI converts this to a non-zero
    exit with the message printed to stderr.
    """


def _is_under_tmp(path: Path) -> bool:
    """True if `path` resolves under `/tmp` (any depth)."""
    resolved = path.expanduser().resolve()
    try:
        # Path.is_relative_to is Python 3.9+; ha-switchbee targets 3.12.
        return resolved.is_relative_to(Path("/tmp").resolve())
    except (ValueError, OSError):
        return False


def check_safety_gates(
    *,
    apply: bool,
    output_dir: Path,
    bridge_config_entry_id: str | None,
    ha_running: bool,
) -> None:
    """Run the three safety gates.

    Args:
        apply: True if the caller is running with `--apply`.
        output_dir: the operator-supplied output directory.
        bridge_config_entry_id: the SwitchBee bridge config_entry_id passed
            via `--bridge-config-entry-id`; None if the operator omitted it.
        ha_running: True if HA is currently running (live container);
            False if HA is stopped or unknown.

    Raises:
        SafetyGateError: if any gate refuses.
    """
    if not apply:
        return

    if not bridge_config_entry_id:
        raise SafetyGateError(
            "--apply requires --bridge-config-entry-id "
            "(needed for device_registry orphan cleanup). "
            "Look up the SwitchBee bridge entry_id in `core.config_entries`."
        )

    if _is_under_tmp(Path(output_dir)):
        raise SafetyGateError(
            f"Refusing --apply with --output-dir under /tmp ({output_dir}). "
            "The backup tarball must survive a reboot (30-day retention rule). "
            "Use a directory under /home/ginnie/ or similar non-tmpfs path."
        )

    if ha_running:
        raise SafetyGateError(
            "HA must be stopped before --apply. "
            "Run: docker compose -f /home/ginnie/ginnie-home/docker-compose.yml stop ginnie-home"
        )


__all__ = ["SafetyGateError", "check_safety_gates"]
