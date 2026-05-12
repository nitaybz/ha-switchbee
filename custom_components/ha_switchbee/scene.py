"""SwitchBee `scene` platform.

SCENARIO and ROLLING_SCENARIO items map to HA `Scene` entities. Per the
source plugin's unified state model, scene activation is the same wire
payload as a switch ON: `OPERATE(itemId, "ON")`. Scenes are stateless on
the HA side; we never query `is_on` because `Scene` has no such concept.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.scene import Scene

from .const import DOMAIN
from .entity import SwitchBeeEntity
from .mapping import map_type_to_platform

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import SwitchBeeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORM: str = "scene"


class SwitchBeeScene(SwitchBeeEntity, Scene):
    """A SwitchBee SCENARIO / ROLLING_SCENARIO exposed as a HA `scene`."""

    async def async_activate(self, **kwargs: Any) -> None:
        """Fire the scene by sending OPERATE(item_id, "ON")."""
        await self.coordinator.client.operate(self._device.id, "ON")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a `SwitchBeeScene` for every SCENARIO / ROLLING_SCENARIO item."""
    coordinator: SwitchBeeCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchBeeScene] = [
        SwitchBeeScene(coordinator, device)
        for device in coordinator.devices.values()
        if map_type_to_platform(device.type) == PLATFORM
    ]
    async_add_entities(entities)


__all__ = ["SwitchBeeScene", "async_setup_entry"]
