#!/usr/bin/env python3
"""ha-switchbee migration CLI.

Migrates an existing `homebridge-switchbee -> homekit_controller` install on
Home Assistant to the new `ha-switchbee` integration in a way that preserves
every entity_id, area_id, icon, alias, and label.

Two-phase apply (plan Decision #8):

    Phase 5a (this script with --apply):
        - back up the four HA registry files + the homebridge config.json
          to `<output-dir>/backup.tar.gz`
        - rewrite `core.entity_registry` per the write-list invariant: for
          every migrated row, set `platform=ha_switchbee`,
          `unique_id={cu_mac}_{item_id}`, `config_entry_id=None`; every
          other field passes through verbatim
        - delete `button.*_identify` rows from `entity_registry`
        - clean orphan rows from `core.device_registry` that reference ONLY
          the SwitchBee bridge config_entry_id

    Phase 5b (HA-side adoption, runs automatically at first ha-switchbee
    setup AFTER the user adds the integration via the HA UI):
        - HA's `EntityRegistry.async_get_or_create((platform, unique_id))`
          matches the orphan rows written by Phase 5a, preserves every
          other field, and fills in `config_entry_id` from the new entry.

Safety gates enforced before any I/O (and BEFORE the backup step):

    - `--apply` requires `--bridge-config-entry-id` (device_registry cleanup)
    - `--apply --output-dir <under /tmp>` is refused (volatile filesystem)
    - `--apply` while HA is running is refused (HA caches registry in memory)

The CU MAC is normally resolved by talking to the live CU. If the operator
passes `--cu-mac <12-hex>` the CLI uses that value directly and skips the
live probe; this is the path the tests and dry-runs against fixtures use.

Examples:
    # Dry-run produces backup + report without touching the registries.
    python tools/migrate.py --dry-run \\
        --ha-storage /home/ginnie/ginnie-home/ha/.storage \\
        --homebridge-persist-dir /var/lib/homebridge/switchbee-persist \\
        --homebridge-config /var/lib/homebridge/config.json \\
        --cu-host 192.168.68.57 --cu-user $SB_USER --cu-pass $SB_PASS \\
        --bridge-mac 0E:0F:B5:1B:3D:37 \\
        --output-dir /home/ginnie/ha-switchbee-migration-$(date +%Y%m%d-%H%M%S)

    # Apply (HA must be stopped first via docker compose stop ginnie-home).
    python tools/migrate.py --apply \\
        ... \\
        --bridge-config-entry-id 01K2M7HFJ8YDYMKW811JCKGC0W
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

# Make `tools._migrate.*` importable when this file is run as a script
# (`python tools/migrate.py`) instead of as a module (`python -m tools.migrate`).
# The pytest path injects the project root automatically, but the subprocess
# path used by the CLI integration tests does not.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools._migrate.applier import EntityRegistryUpdate, apply_entity_registry  # noqa: E402
from tools._migrate.backup import BackupInputError, create_backup  # noqa: E402
from tools._migrate.device_registry_cleaner import clean_orphan_devices  # noqa: E402
from tools._migrate.homebridge_reader import (  # noqa: E402
    HomebridgePersistNotFoundError,
    load_switchbee_configuration,
)
from tools._migrate.mapper import map_entities  # noqa: E402
from tools._migrate.registry_reader import (  # noqa: E402, F401
    UnsupportedRegistryVersionError,
    filter_homekit_switchbee,
    load_area_registry,
    load_device_registry,
    load_entity_registry,
)
from tools._migrate.report import write_reports  # noqa: E402
from tools._migrate.safety import SafetyGateError, check_safety_gates  # noqa: E402

_LOGGER = logging.getLogger("ha_switchbee.migrate")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Public so tests can introspect the flag set without spawning a subprocess.
    """
    parser = argparse.ArgumentParser(
        prog="migrate.py",
        description="Migrate homebridge-switchbee entity rows onto ha-switchbee.",
    )
    parser.add_argument(
        "--ha-storage",
        required=True,
        type=Path,
        help="path to HA .storage/ directory",
    )
    parser.add_argument(
        "--homebridge-persist-dir",
        required=True,
        type=Path,
        help="path to homebridge node-persist dir with the SwitchBee plugin cache",
    )
    parser.add_argument(
        "--homebridge-config",
        required=True,
        type=Path,
        help="path to /var/lib/homebridge/config.json (snapshotted into backup)",
    )
    parser.add_argument(
        "--cu-host",
        required=True,
        help="SwitchBee Central Unit host or IP",
    )
    parser.add_argument(
        "--cu-user",
        required=True,
        help="SwitchBee CU username",
    )
    parser.add_argument(
        "--cu-pass",
        required=True,
        help="SwitchBee CU password (prefer $SB_PASS env var)",
    )
    parser.add_argument(
        "--cu-mac",
        default=None,
        help="lowercase 12-hex CU MAC; when omitted the CLI probes the live CU",
    )
    parser.add_argument(
        "--bridge-mac",
        required=True,
        help="homekit_controller bridge MAC prefix on existing unique_ids",
    )
    parser.add_argument(
        "--bridge-config-entry-id",
        default=None,
        help="SwitchBee bridge config_entry_id (required for --apply)",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="dir for backup.tar.gz + reports (must NOT be under /tmp for --apply)",
    )
    parser.add_argument(
        "--ha-stopped-check",
        default="docker-compose",
        help=(
            "how to verify HA is stopped before --apply: "
            "'docker-compose' (probe /home/ginnie/ginnie-home), "
            "'skip' (operator asserts HA is stopped; intended for tests)"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="produce backup + report only; default",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="rewrite entity_registry + device_registry in place",
    )
    return parser


def _probe_ha_running(mode: str) -> bool:
    """Return True if HA appears to be running, False if stopped or unknown.

    `mode == "skip"` returns False unconditionally (operator-asserted).
    Other values shell out to docker compose to inspect the container.
    """
    if mode == "skip":
        return False
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "/home/ginnie/ginnie-home/docker-compose.yml",
                "ps",
                "ginnie-home",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            _LOGGER.warning("docker compose ps failed; assuming HA is running: %s", result.stderr)
            return True
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            state = (doc.get("State") or "").lower()
            if state and state != "exited":
                return True
        return False
    except (OSError, subprocess.TimeoutExpired) as err:
        _LOGGER.warning("HA-running probe failed; assuming HA is running: %s", err)
        return True


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the desired process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    apply_mode = bool(args.apply)
    if apply_mode:
        # When --apply is passed explicitly, dry-run flips off even though
        # argparse defaults `--dry-run=True`.
        args.dry_run = False

    ha_running = False
    if apply_mode:
        ha_running = _probe_ha_running(args.ha_stopped_check)

    try:
        check_safety_gates(
            apply=apply_mode,
            output_dir=args.output_dir,
            bridge_config_entry_id=args.bridge_config_entry_id,
            ha_running=ha_running,
        )
    except SafetyGateError as err:
        print(f"safety gate refused: {err}", file=sys.stderr)
        return 2

    # 1. Always create the backup FIRST (P1 invariant).
    try:
        backup = create_backup(
            ha_storage=args.ha_storage,
            homebridge_config_path=args.homebridge_config,
            output_dir=args.output_dir,
        )
    except BackupInputError as err:
        print(f"backup refused: {err}", file=sys.stderr)
        return 3
    _LOGGER.info("backup written to %s", backup)

    # 2. Load registries.
    try:
        er_raw = load_entity_registry(args.ha_storage / "core.entity_registry")
    except UnsupportedRegistryVersionError as err:
        print(f"registry version refused: {err}", file=sys.stderr)
        return 4
    entities = er_raw["data"]["entities"]
    homekit_entries = filter_homekit_switchbee(entities, bridge_mac=args.bridge_mac)

    # 3. Load homebridge persist (CU device map).
    try:
        cu_devices = load_switchbee_configuration(args.homebridge_persist_dir)
    except HomebridgePersistNotFoundError as err:
        print(f"homebridge persist load failed: {err}", file=sys.stderr)
        return 5

    # 4. Resolve CU MAC.
    cu_mac = args.cu_mac
    if cu_mac is None:
        print(
            "--cu-mac is required for now (live CU probe deferred to v1.1). "
            "Look up the MAC from `GET_CONFIGURATION.data.mac` and pass it "
            "as a 12-hex lowercase string.",
            file=sys.stderr,
        )
        return 6

    # 5. Build the mapping. PRIMARY path is SerialNumber-based via HA's
    # device_registry; the legacy name-match path is the fallback when SN
    # is absent. Loading ha_devices makes the mapper rename-proof against
    # any CU-side name changes after pairing.
    area_registry = load_area_registry(args.ha_storage / "core.area_registry")
    ha_devices = load_device_registry(args.ha_storage / "core.device_registry")
    rows = map_entities(
        homekit_entries,
        cu_devices=cu_devices,
        cu_mac=cu_mac,
        bridge_mac=args.bridge_mac,
        area_registry=area_registry,
        ha_devices=ha_devices,
    )

    # 6. Write reports.
    write_reports(rows, args.output_dir)

    if not apply_mode:
        _LOGGER.info(
            "dry-run complete: backup=%s reports in %s; %d rows mapped",
            backup,
            args.output_dir,
            len(rows),
        )
        return 0

    # 7. Apply.
    update = EntityRegistryUpdate(
        rewrites=[r for r in rows if r.action == "migrate" and r.confidence in {"high", "medium"}],
        deletes=[r for r in rows if r.action == "delete"],
    )
    summary_entity = apply_entity_registry(args.ha_storage / "core.entity_registry", update)
    summary_device = clean_orphan_devices(
        args.ha_storage / "core.device_registry",
        bridge_config_entry_id=args.bridge_config_entry_id,
    )

    apply_result = {
        "entity_registry": {
            "migrated": summary_entity.migrated,
            "deleted": summary_entity.deleted,
            "untouched": summary_entity.untouched,
        },
        "device_registry": {
            "deleted": summary_device.deleted,
            "untouched": summary_device.untouched,
        },
        "backup_path": str(backup),
    }
    (args.output_dir / "apply-result.json").write_text(json.dumps(apply_result, indent=2))
    _LOGGER.info(
        "--apply complete: entity migrated=%d deleted=%d, device deleted=%d",
        summary_entity.migrated,
        summary_entity.deleted,
        summary_device.deleted,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
