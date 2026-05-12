"""Reader for the homebridge plugin's node-persist cache.

`homebridge-switchbee` uses node-persist to cache a flat device map under
the key `switchbee-configuration`. The filename is a node-persist hash of
the key, which varies per install; the tool MUST scan all files in the
directory and pick the one whose JSON `key` field equals
`"switchbee-configuration"`.

The `value` of that record is the same map shape returned by
`SwitchBee/api.js:getDevices()`:

    {item_id: {id, name, hw, type, zone}, ...}

Other files in the same directory (`switchbee-token`, `switchbee-raw-state`,
node-persist housekeeping files) are ignored.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

CONFIG_KEY = "switchbee-configuration"


class HomebridgePersistNotFoundError(RuntimeError):
    """No file in the persist dir has `key == "switchbee-configuration"`."""


def load_switchbee_configuration(persist_dir: Path) -> Mapping[int, Mapping[str, Any]]:
    """Return the flat homebridge `{item_id: {id, name, hw, type, zone}, ...}` map.

    Args:
        persist_dir: path to the homebridge node-persist directory.

    Raises:
        HomebridgePersistNotFoundError: if no file with the configuration key
            is found in the directory.
    """
    persist = Path(persist_dir)
    if not persist.is_dir():
        raise HomebridgePersistNotFoundError(f"persist directory not found: {persist_dir}")
    for entry in persist.iterdir():
        if not entry.is_file():
            continue
        try:
            doc = json.loads(entry.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(doc, Mapping):
            continue
        if doc.get("key") != CONFIG_KEY:
            continue
        value = doc.get("value")
        if not isinstance(value, Mapping):
            raise HomebridgePersistNotFoundError(
                f"file {entry} has key={CONFIG_KEY!r} but its `value` is not a map"
            )
        # node-persist serializes int keys as strings in JSON. Coerce.
        result: dict[int, Mapping[str, Any]] = {}
        for key, val in value.items():
            try:
                result[int(key)] = val
            except (TypeError, ValueError):
                continue
        return result
    raise HomebridgePersistNotFoundError(
        f"no file in {persist_dir!r} has key == {CONFIG_KEY!r}; "
        "is homebridge-switchbee installed and has it ever fetched a configuration?"
    )


__all__ = ["CONFIG_KEY", "HomebridgePersistNotFoundError", "load_switchbee_configuration"]
