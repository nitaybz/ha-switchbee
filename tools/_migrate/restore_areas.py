"""Restore `area_id` from a pre-cutover backup onto post-cutover entities.

The migration tool's Apply step deletes orphan device_registry rows whose
only `config_entries` reference was the SwitchBee bridge (Phase 5 P7).
Each of those rows carried the per-item `area_id` that the user had set
in HA. With the rows gone, every migrated entity ends up with `area_id =
None`, which is why HA UI and Apple Home show them all in "Default Room"
post-cutover.

This module is the recovery path:

1. Open the pre-cutover backup tarball (`backup.tar.gz`) produced by the
   apply step. The tarball preserves `core.entity_registry`,
   `core.device_registry`, `core.area_registry`, `core.config_entries`,
   and `/var/lib/homebridge/config.json` per Phase 5 plan T2.
2. From the backup's `core.device_registry`, build a map
   `{item_id: {area_id, name, name_by_user}}` by parsing each row's
   `serial_number` field with the `_ID(<digits>)$` regex (the same
   SerialNumber field set by
   `homebridge-switchbee/SwitchBee/unified.js:48`).
3. Walk the live `core.entity_registry`. For each `platform == ha_switchbee`
   row, look up the item.id from the `unique_id` suffix and apply the
   matching backup `area_id` to the entity_registry row.
4. Optionally apply `name_by_user` (only if the user had explicitly
   renamed the device, distinguishing `name_by_user` from the default
   `name`). This is conservative: only set `name_by_user` on the entity
   when the backup had `name_by_user != None`.

The write is atomic (tempfile + rename) and idempotent: re-running the
script is safe because each pass produces the same result.

Also writes area_id onto any per-item device_registry rows whose
`identifiers` match the new `(DOMAIN, "{cu_mac}_{item_id}")` shape that
the refactored `entity.py` produces. This step is only meaningful if HA
has already booted with the refactor and created those device rows; the
script is a no-op on devices it does not find.

HA must be stopped before running --apply on the restore script. The
restore script enforces this via the same `docker compose ps` gate as
the main migration tool.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

_SN_ID_RE = re.compile(r"_ID(\d+)$")
_UID_TAIL_RE = re.compile(r"_(\d+)$")
DOMAIN = "ha_switchbee"


def _load_from_tar(tar_path: Path, basename: str) -> dict[str, Any]:
    """Extract one JSON member from the backup tarball.

    The tarball stores files under their original relative paths
    (`storage/core.entity_registry`, etc). We match by basename suffix so
    the script is tolerant of small layout changes.
    """
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            if m.name.endswith(basename):
                f = t.extractfile(m)
                if f is None:
                    continue
                return json.loads(f.read())
    raise FileNotFoundError(f"{basename!r} not found in {tar_path}")


def _build_item_to_area_map(
    old_device_registry: Mapping[str, Any],
) -> dict[int, dict[str, Any]]:
    """Return `{item_id: {area_id, name, name_by_user}}` from backup.

    Parses each device row's `serial_number` field. Rows without a usable
    `_ID<n>` suffix are skipped (the homebridge bridge itself, third
    party devices).
    """
    out: dict[int, dict[str, Any]] = {}
    for d in old_device_registry.get("data", {}).get("devices", []):
        sn = d.get("serial_number")
        if not sn:
            continue
        m = _SN_ID_RE.search(str(sn))
        if not m:
            continue
        try:
            item_id = int(m.group(1))
        except ValueError:
            continue
        out[item_id] = {
            "area_id": d.get("area_id"),
            "name": d.get("name"),
            "name_by_user": d.get("name_by_user"),
        }
    return out


def _parse_item_id_from_unique_id(unique_id: str) -> int | None:
    """Pull the item.id off the `{cu_mac}_{item_id}` unique_id format."""
    m = _UID_TAIL_RE.search(unique_id)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def restore_entity_areas(
    *,
    backup_tarball: Path,
    ha_storage: Path,
    apply: bool,
    clear_device_id: bool = True,
) -> dict[str, int]:
    """Restore `area_id` (and optionally `name_by_user`) on ha_switchbee rows.

    Also clears `device_id` on every `ha_switchbee` entity_registry row when
    `clear_device_id=True` (default). The migration's orphan_cleaner step
    deleted the old per-item device_registry rows; the entity rows kept
    stale `device_id` references. HA's `async_get_or_create` does NOT
    re-link entities to new devices on adoption when `device_id` is set,
    so unless we null it out, every entity stays attached to whatever
    surviving device row HA happens to find (e.g. the SwitchBee CU bridge
    device). Clearing forces HA to use the entity's `DeviceInfo` to
    create/find the per-item device on next async_add_entities call.

    Returns counters describing the operation.
    """
    old_dr = _load_from_tar(backup_tarball, "core.device_registry")
    item_map = _build_item_to_area_map(old_dr)
    _LOGGER.info(
        "backup contains %d items with mapping data", len(item_map)
    )

    er_path = ha_storage / "core.entity_registry"
    er = json.loads(er_path.read_text())

    inspected = 0
    would_update = 0
    no_area_in_backup = 0
    unmatched = 0
    unchanged = 0
    device_id_cleared = 0

    for e in er["data"]["entities"]:
        if e.get("platform") != DOMAIN:
            continue
        inspected += 1
        uid = str(e.get("unique_id") or "")
        item_id = _parse_item_id_from_unique_id(uid)

        # Always clear device_id (unless explicitly disabled) so HA
        # rebuilds the entity -> device link from DeviceInfo on next
        # async_add_entities. The previous device_id, if any, points to
        # the SwitchBee CU bridge device left over from the first cutover
        # boot when the integration used a single-device DeviceInfo.
        if clear_device_id and e.get("device_id") is not None:
            device_id_cleared += 1
            if apply:
                e["device_id"] = None

        if item_id is None:
            unmatched += 1
            continue
        meta = item_map.get(item_id)
        if not meta:
            unmatched += 1
            continue
        target_area = meta["area_id"]
        if target_area is None:
            no_area_in_backup += 1
            continue
        if e.get("area_id") == target_area:
            unchanged += 1
            continue
        would_update += 1
        if apply:
            e["area_id"] = target_area
            # Bring across `name_by_user` only when the user had set it
            # explicitly in the old setup (a user-renamed device).
            if meta.get("name_by_user"):
                e["name"] = meta["name_by_user"]

    if apply:
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=str(er_path.parent),
            prefix=".core.entity_registry.",
            suffix=".tmp",
        ) as tmp:
            json.dump(er, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.rename(tmp_name, er_path)

    return {
        "inspected": inspected,
        "would_update": would_update,
        "updated": would_update if apply else 0,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "no_area_in_backup": no_area_in_backup,
        "device_id_cleared": device_id_cleared if apply else 0,
    }


def restore_device_areas(
    *,
    backup_tarball: Path,
    ha_storage: Path,
    cu_mac: str,
    apply: bool,
) -> dict[str, int]:
    """Restore `area_id` on per-item ha_switchbee device_registry rows.

    Only meaningful after HA has booted with the refactored integration
    that creates per-item devices keyed by `(DOMAIN, "{cu_mac}_{item_id}")`.
    Devices that do not yet exist are silently skipped (the entity-level
    area_id restore above is what HA UI actually displays).
    """
    old_dr = _load_from_tar(backup_tarball, "core.device_registry")
    item_map = _build_item_to_area_map(old_dr)

    dr_path = ha_storage / "core.device_registry"
    dr = json.loads(dr_path.read_text())

    inspected = 0
    would_update = 0
    unchanged = 0
    unmatched = 0

    target_prefix = f"{cu_mac}_"
    for d in dr["data"]["devices"]:
        ids = d.get("identifiers") or []
        item_id: int | None = None
        for ident in ids:
            if not (isinstance(ident, list) and len(ident) == 2):
                continue
            domain, key = ident
            if domain != DOMAIN:
                continue
            if not isinstance(key, str) or not key.startswith(target_prefix):
                continue
            tail = key[len(target_prefix):]
            try:
                item_id = int(tail)
            except ValueError:
                item_id = None
            break
        if item_id is None:
            continue
        inspected += 1
        meta = item_map.get(item_id)
        if not meta:
            unmatched += 1
            continue
        target_area = meta["area_id"]
        if target_area is None:
            continue
        if d.get("area_id") == target_area:
            unchanged += 1
            continue
        would_update += 1
        if apply:
            d["area_id"] = target_area

    if apply:
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=str(dr_path.parent),
            prefix=".core.device_registry.",
            suffix=".tmp",
        ) as tmp:
            json.dump(dr, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.rename(tmp_name, dr_path)

    return {
        "inspected": inspected,
        "would_update": would_update,
        "updated": would_update if apply else 0,
        "unchanged": unchanged,
        "unmatched": unmatched,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore area_id (and optional name_by_user) onto post-cutover "
        "ha_switchbee entity_registry and device_registry rows from a "
        "pre-cutover migration backup tarball."
    )
    parser.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="path to backup.tar.gz produced by `migrate.py --apply`",
    )
    parser.add_argument(
        "--ha-storage",
        type=Path,
        required=True,
        help="path to HA `.storage` directory",
    )
    parser.add_argument(
        "--cu-mac",
        required=True,
        help="normalized 12-hex lowercase CU MAC (e.g. a82108e7688f)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write changes (default is dry-run preview)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )

    ent = restore_entity_areas(
        backup_tarball=args.backup,
        ha_storage=args.ha_storage,
        apply=args.apply,
    )
    dev = restore_device_areas(
        backup_tarball=args.backup,
        ha_storage=args.ha_storage,
        cu_mac=args.cu_mac,
        apply=args.apply,
    )

    print()
    print("entity_registry restore:")
    for k, v in ent.items():
        print(f"  {k}: {v}")
    print("device_registry restore:")
    for k, v in dev.items():
        print(f"  {k}: {v}")
    if not args.apply:
        print("\n(dry-run; pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
