"""Phase 5a Step 3: device_registry orphan cleanup (P7).

After the entity_registry rewrite deletes the 78 `button.*_identify` rows,
the 78 corresponding device_registry rows are left referencing only the
SwitchBee homekit_controller bridge config_entry. Without cleanup, HA's UI
would render 78 ghost device cards labelled "Accessory 75" etc. after
cutover.

Algorithm:
- Load `core.device_registry`.
- A device row is an orphan iff `config_entries == [bridge_config_entry_id]`
  (exactly one entry, exactly the SwitchBee bridge).
- Remove orphan rows. Pass every other row through verbatim.
- Re-serialize preserving `version`, `minor_version`, `key`, and `data`
  subkeys like `deleted_devices`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceRegistryCleanSummary:
    """Counts emitted by `clean_orphan_devices`."""

    deleted: int = 0
    untouched: int = 0


def clean_orphan_devices(
    registry_path: Path,
    *,
    bridge_config_entry_id: str,
) -> DeviceRegistryCleanSummary:
    """Remove device_registry rows orphaned by the entity_registry rewrite.

    Args:
        registry_path: path to `core.device_registry`.
        bridge_config_entry_id: the config_entry_id of the SwitchBee
            homekit_controller bridge (verified live on Moshe's device).

    Returns:
        Per-run counts.
    """
    registry_path = Path(registry_path)
    raw = json.loads(registry_path.read_text())
    data = raw.setdefault("data", {})
    devices = list(data.get("devices", []))

    kept: list[dict] = []
    deleted = 0
    for dev in devices:
        config_entries = dev.get("config_entries") or []
        if list(config_entries) == [bridge_config_entry_id]:
            deleted += 1
            continue
        kept.append(dev)
    untouched = len(kept)
    data["devices"] = kept

    tmp = registry_path.with_name(registry_path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    serialized = json.dumps(raw, ensure_ascii=False, indent=2)
    tmp.write_text(serialized)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, registry_path)

    _LOGGER.info("device_registry cleaned: deleted=%d untouched=%d", deleted, untouched)
    return DeviceRegistryCleanSummary(deleted=deleted, untouched=untouched)


__all__ = ["DeviceRegistryCleanSummary", "clean_orphan_devices"]
