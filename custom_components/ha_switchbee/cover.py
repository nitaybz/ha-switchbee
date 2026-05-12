"""SwitchBee `cover` platform.

Handles three SwitchBee types under a single public class
`SwitchBeeCover`:

- SHUTTER / LOUVERED_SHUTTER: percent-based covers. The CU stores the
  current position as 0-100 (0 = fully closed, 100 = fully open) and
  accepts the same scale in OPERATE.value.
- SOMFY: command-only covers with no position feedback. The CU accepts
  the verbs `UP`, `DOWN`, `STOP` in OPERATE.value and emits the same
  verbs as state.

The plan locks the class name; internal branching by `device.type` keeps
the public surface small. Tilt control for LOUVERED_SHUTTER is deferred
to v1.1.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)

from .const import DOMAIN
from .entity import SwitchBeeEntity
from .mapping import map_type_to_platform
from .models import decode_shutter, encode_shutter, encode_somfy

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import SwitchBeeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORM: str = "cover"

# SwitchBee cover types that report a numeric percent position.
_PERCENT_TYPES: frozenset[str] = frozenset({"SHUTTER", "LOUVERED_SHUTTER"})
# SwitchBee command-based cover types (no position state).
_COMMAND_TYPES: frozenset[str] = frozenset({"SOMFY"})


class SwitchBeeCover(SwitchBeeEntity, CoverEntity):
    """A SwitchBee cover entity, branching by device type.

    For SHUTTER / LOUVERED_SHUTTER we expose `current_cover_position`
    and `set_cover_position`; for SOMFY we expose only OPEN / CLOSE /
    STOP.
    """

    def __init__(self, coordinator, device) -> None:  # type: ignore[no-untyped-def]
        super().__init__(coordinator, device)
        if device.type in _PERCENT_TYPES:
            self._attr_supported_features = (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
                | CoverEntityFeature.SET_POSITION
            )
        else:
            self._attr_supported_features = (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
            )

    @property
    def _is_percent_type(self) -> bool:
        return self._device.type in _PERCENT_TYPES

    @property
    def current_cover_position(self) -> int | None:
        """0-100 for SHUTTER family; None for SOMFY."""
        if not self._is_percent_type:
            return None
        raw = self.coordinator.data.get(self._device.id)
        if not isinstance(raw, (int, float)):
            return None
        return decode_shutter(int(raw))

    @property
    def is_closed(self) -> bool | None:
        """True when the percent position is 0; unknown for SOMFY."""
        if not self._is_percent_type:
            # SOMFY has no position feedback; HA accepts None.
            return None
        position = self.current_cover_position
        if position is None:
            return None
        return position == 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        if self._is_percent_type:
            await self.coordinator.client.operate(self._device.id, 100)
        else:
            await self.coordinator.client.operate(
                self._device.id, encode_somfy("UP")
            )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        if self._is_percent_type:
            await self.coordinator.client.operate(self._device.id, 0)
        else:
            await self.coordinator.client.operate(
                self._device.id, encode_somfy("DOWN")
            )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover. Same payload for both flavours."""
        if self._is_percent_type:
            await self.coordinator.client.operate(self._device.id, "STOP")
        else:
            await self.coordinator.client.operate(
                self._device.id, encode_somfy("STOP")
            )

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set a specific position. Only valid for SHUTTER family."""
        if not self._is_percent_type:
            return
        position = kwargs.get(ATTR_POSITION)
        if position is None:
            return
        value = encode_shutter(int(position))
        await self.coordinator.client.operate(self._device.id, value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a `SwitchBeeCover` for every cover-family CU item."""
    coordinator: SwitchBeeCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchBeeCover] = [
        SwitchBeeCover(coordinator, device)
        for device in coordinator.devices.values()
        if map_type_to_platform(device.type) == PLATFORM
    ]
    async_add_entities(entities)


__all__ = ["SwitchBeeCover", "async_setup_entry"]
