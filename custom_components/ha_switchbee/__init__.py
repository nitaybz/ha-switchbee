"""SwitchBee Local custom integration.

Phase 0 stub. The real `async_setup_entry` implementation, the WebSocket
client, and the platform fan-out land in Phase 3 once the protocol module
(Phase 1) and entity model (Phase 2) are in place.

Note on imports: `homeassistant` is only imported under `TYPE_CHECKING`
so that this module is importable in environments where Home Assistant is
not installed (for example, the Phase 0 smoke test running under a plain
Python 3.12 venv). At runtime inside HA, the package is naturally on
sys.path and the type hints are still meaningful via the deferred import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

__all__ = ["DOMAIN", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SwitchBee Local from a config entry.

    Full implementation lands in Phase 3.
    """
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
