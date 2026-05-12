"""Migration report writers.

`write_reports` emits two artifacts to the output directory:

- `report.json`: machine-readable, one row per source entity plus a
  summary count block.
- `report.md`: human-readable markdown for the operator to review before
  passing `--apply`.

Both formats include every input entity so the operator can audit the
keep-on-purpose (SENSOR / TWO_WAY) and delete (button.*_identify) rows
alongside the migrate rows.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from .mapper import MappingRow


def write_reports(rows: Iterable[MappingRow], output_dir: Path) -> tuple[Path, Path]:
    """Emit `report.json` and `report.md` to `output_dir`.

    Returns the two paths.
    """
    rows = list(rows)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = Counter(r.action for r in rows)
    summary = {
        "total": len(rows),
        "migrate": counts.get("migrate", 0),
        "delete": counts.get("delete", 0),
        "keep_homekit": counts.get("keep_homekit", 0),
        "high_confidence": sum(1 for r in rows if r.confidence == "high"),
        "medium_confidence": sum(1 for r in rows if r.confidence == "medium"),
        "low_confidence": sum(1 for r in rows if r.confidence == "low"),
    }
    payload = {
        "summary": summary,
        "rows": [
            {
                "entity_id": r.entity_id,
                "old_unique_id": r.old_unique_id,
                "new_unique_id": r.new_unique_id,
                "confidence": r.confidence,
                "action": r.action,
                "reason": r.reason,
                "item_id": r.item_id,
                "sb_type": r.sb_type,
            }
            for r in rows
        ],
    }
    json_path = output_dir / "report.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    md_lines = [
        "# ha-switchbee migration report",
        "",
        "## Summary",
        f"- total: {summary['total']}",
        f"- migrate: {summary['migrate']}",
        f"- delete: {summary['delete']}",
        f"- keep_homekit: {summary['keep_homekit']}",
        f"- high confidence: {summary['high_confidence']}",
        f"- medium confidence: {summary['medium_confidence']}",
        f"- low confidence: {summary['low_confidence']}",
        "",
        "## Rows",
        "",
        "| entity_id | action | confidence | old_unique_id | new_unique_id | reason |",
        "|-----------|--------|------------|---------------|---------------|--------|",
    ]
    for r in rows:
        md_lines.append(
            f"| {r.entity_id} | {r.action} | {r.confidence} | "
            f"{r.old_unique_id} | {r.new_unique_id or ''} | {r.reason} |"
        )
    md_path = output_dir / "report.md"
    md_path.write_text("\n".join(md_lines) + "\n")
    return json_path, md_path


__all__ = ["write_reports"]
