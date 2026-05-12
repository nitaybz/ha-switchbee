"""Phase 5a applier: rewrite `core.entity_registry`.

Per Decision #13 (P8 / write-list invariant) the applier mutates ONLY three
fields per migrated row:

    row["platform"] = "ha_switchbee"
    row["unique_id"] = f"{cu_mac}_{item_id}"
    row["config_entry_id"] = None

Every other field is passed through verbatim. Implementation MUST use the
three-line assignment style above and NEVER a `{k: v for k in keep_list}`
comprehension, which would drop unknown fields (e.g. new minor-version
fields HA may add later).

`button.*_identify` rows are removed from the `entities` array entirely.

Top-level structure (`version`, `minor_version`, `key`, any extra top-level
keys, the `data` dict's extra subkeys like `deleted_entities`) is preserved
byte-identical.

Write order: load -> mutate -> serialize to `.tmp` -> fsync -> atomic rename.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mapper import MappingRow

_LOGGER = logging.getLogger(__name__)


@dataclass
class EntityRegistryUpdate:
    """Bundle of mapper rows to apply.

    `rewrites` contains rows with `action == "migrate"` and confidence in
    `{"high", "medium"}`. `deletes` contains rows with `action == "delete"`.
    `low` / `keep_homekit` rows are NOT included; they are emitted to the
    report only.
    """

    rewrites: list[MappingRow] = field(default_factory=list)
    deletes: list[MappingRow] = field(default_factory=list)


@dataclass
class ApplyEntitySummary:
    """Counts emitted by `apply_entity_registry` for the reconciliation report."""

    migrated: int = 0
    deleted: int = 0
    untouched: int = 0


def apply_entity_registry(
    registry_path: Path,
    update: EntityRegistryUpdate,
) -> ApplyEntitySummary:
    """Rewrite `core.entity_registry` in place.

    Args:
        registry_path: path to the live `core.entity_registry` JSON file.
        update: rows to rewrite + rows to delete.

    Returns:
        `ApplyEntitySummary` with counts for the post-apply report.
    """
    registry_path = Path(registry_path)
    raw = json.loads(registry_path.read_text())
    data = raw.setdefault("data", {})
    entities: list[dict[str, Any]] = list(data.get("entities", []))

    rewrites_by_entity_id = {r.entity_id: r for r in update.rewrites}
    delete_entity_ids = {r.entity_id for r in update.deletes}

    new_entities: list[dict[str, Any]] = []
    migrated = 0
    deleted = 0
    untouched = 0
    for row in entities:
        entity_id = row.get("entity_id")
        if entity_id in delete_entity_ids:
            deleted += 1
            continue
        rewrite = rewrites_by_entity_id.get(entity_id)
        if rewrite is not None and rewrite.new_unique_id is not None:
            # P8: mutate ONLY these three fields. NEVER a keep-list comprehension.
            row["platform"] = "ha_switchbee"
            row["unique_id"] = rewrite.new_unique_id
            row["config_entry_id"] = None
            migrated += 1
        else:
            untouched += 1
        new_entities.append(row)

    data["entities"] = new_entities

    # Re-serialize to `.tmp` then atomic rename.
    tmp = registry_path.with_name(registry_path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    serialized = json.dumps(raw, ensure_ascii=False, indent=2)
    tmp.write_text(serialized)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, registry_path)

    _LOGGER.info(
        "entity_registry rewritten: migrated=%d deleted=%d untouched=%d",
        migrated,
        deleted,
        untouched,
    )
    return ApplyEntitySummary(migrated=migrated, deleted=deleted, untouched=untouched)


__all__ = ["ApplyEntitySummary", "EntityRegistryUpdate", "apply_entity_registry"]
