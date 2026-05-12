"""SwitchBee `switch` platform.

Maps every CU item whose type lands on the `switch` platform per
`mapping.MAPPING_TABLE` (SWITCH, TIMED_SWITCH, GROUP_SWITCH, TIMED_POWER,
LOCK_GROUP) to a `SwitchEntity`. Service calls translate to OPERATE
payloads with `"ON"` or `"OFF"` directly; the CU echoes the change as a
CONFIGURATION_CHANGE push, so we never set optimistic state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity

from .const import DOMAIN
from .entity import SwitchBeeEntity
from .mapping import map_type_to_platform
from .models import decode_on_off

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import SwitchBeeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORM: str = "switch"


class SwitchBeeSwitch(SwitchBeeEntity, SwitchEntity):
    """A SwitchBee item exposed as a HA `switch` entity."""

    @property
    def is_on(self) -> bool:
        """True if the cached state decodes to ON."""
        raw = self.coordinator.data.get(self._device.id)
        return decode_on_off(raw if isinstance(raw, str) else "")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send OPERATE(item_id, "ON")."""
        await self.coordinator.client.operate(self._device.id, "ON")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send OPERATE(item_id, "OFF")."""
        await self.coordinator.client.operate(self._device.id, "OFF")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a `SwitchBeeSwitch` for every switch-family CU item."""
    coordinator: SwitchBeeCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchBeeSwitch] = [
        SwitchBeeSwitch(coordinator, device)
        for device in coordinator.devices.values()
        if map_type_to_platform(device.type) == PLATFORM
    ]
    async_add_entities(entities)


__all__ = ["SwitchBeeSwitch", "async_setup_entry"]
