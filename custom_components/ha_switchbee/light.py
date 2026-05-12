"""SwitchBee `light` platform (DIMMER items only).

HA brightness is on a 0-255 byte scale; SwitchBee DIMMER values are on a
0-100 percent scale on the wire. This module converts between the two,
rounding to the nearest integer percent on the way out and to the nearest
byte on the way in.

`async_turn_on` without an explicit brightness defaults to 100 (full on),
matching the HA convention for switchable lights.

`async_turn_off` sends `OPERATE(item_id, 0)` because SwitchBee DIMMER off
is encoded as percent 0, not the string `"OFF"`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity

from .const import DOMAIN
from .entity import SwitchBeeEntity
from .mapping import map_type_to_platform
from .models import decode_dimmer, encode_dimmer

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import SwitchBeeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORM: str = "light"

# HA-to-SwitchBee byte/percent conversion.
_HA_BRIGHTNESS_MAX: int = 255
_CU_PERCENT_MAX: int = 100


def _ha_byte_to_cu_percent(ha_brightness: int) -> int:
    """Map HA 0-255 to SwitchBee 0-100 percent, clamped."""
    if ha_brightness <= 0:
        return 0
    if ha_brightness >= _HA_BRIGHTNESS_MAX:
        return _CU_PERCENT_MAX
    return round(ha_brightness * _CU_PERCENT_MAX / _HA_BRIGHTNESS_MAX)


def _cu_percent_to_ha_byte(cu_percent: int) -> int:
    """Map SwitchBee 0-100 percent to HA 0-255 byte, clamped."""
    if cu_percent <= 0:
        return 0
    if cu_percent >= _CU_PERCENT_MAX:
        return _HA_BRIGHTNESS_MAX
    return round(cu_percent * _HA_BRIGHTNESS_MAX / _CU_PERCENT_MAX)


class SwitchBeeLight(SwitchBeeEntity, LightEntity):
    """A SwitchBee DIMMER exposed as a HA `light` entity."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    @property
    def is_on(self) -> bool:
        """True when the cached percent is greater than zero."""
        raw = self.coordinator.data.get(self._device.id)
        if not isinstance(raw, (int, float)):
            return False
        return decode_dimmer(int(raw)) > 0

    @property
    def brightness(self) -> int | None:
        """Current brightness on the HA 0-255 scale, or None if unknown."""
        raw = self.coordinator.data.get(self._device.id)
        if not isinstance(raw, (int, float)):
            return None
        return _cu_percent_to_ha_byte(decode_dimmer(int(raw)))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set brightness. With no `brightness` kwarg, send 100% (full on)."""
        ha_brightness = kwargs.get(ATTR_BRIGHTNESS)
        if ha_brightness is None:
            cu_percent = _CU_PERCENT_MAX
        else:
            cu_percent = encode_dimmer(_ha_byte_to_cu_percent(int(ha_brightness)))
        await self.coordinator.client.operate(self._device.id, cu_percent)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send OPERATE(item_id, 0)."""
        await self.coordinator.client.operate(self._device.id, 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a `SwitchBeeLight` for every DIMMER CU item."""
    coordinator: SwitchBeeCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchBeeLight] = [
        SwitchBeeLight(coordinator, device)
        for device in coordinator.devices.values()
        if map_type_to_platform(device.type) == PLATFORM
    ]
    async_add_entities(entities)


__all__ = ["SwitchBeeLight", "async_setup_entry"]
