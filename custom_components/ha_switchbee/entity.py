"""Shared base entity for every SwitchBee Local platform.

`SwitchBeeEntity` wires:
- a stable `unique_id` in the format `{cu_mac}_{item_id}` (the format
  locked in Phase 3; cu_mac is the lowercase 12-hex normalized CU MAC).
- a `DeviceInfo` with `identifiers = {(DOMAIN, cu_mac)}` so every entity
  rolls up onto a single SwitchBee Central Unit device card in HA.
- an `available` property that is False when the WS is disconnected OR
  when the cached state is one of the OFFLINE sentinels emitted by the
  CU (`-1` or `"OFFLINE"`).
- subscription to the per-item dispatcher signal so push events update
  the entity within ~1s of the CU emitting CONFIGURATION_CHANGE.

The class is intentionally minimal. Platform-specific logic (is_on,
brightness, position, scene activation) lives in the per-platform
modules.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityPlatformState
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import SwitchBeeCoordinator
    from .models import SwitchBeeDevice

_LOGGER = logging.getLogger(__name__)

# Raw CU values that mean "device is unreachable / has no last-known state".
# Both forms have been observed in the wild; the platform layer must treat
# either as "unavailable" rather than "OFF" or position 0.
_OFFLINE_SENTINELS: frozenset[object] = frozenset({-1, "OFFLINE"})


class SwitchBeeEntity(CoordinatorEntity["SwitchBeeCoordinator"]):
    """Base entity for every SwitchBee platform module.

    Concrete subclasses provide platform-specific state translation
    (is_on, brightness, current_cover_position, ...) and the actions
    that call `coordinator.client.operate(...)`.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SwitchBeeCoordinator,
        device: SwitchBeeDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        cu_mac = coordinator.cu_mac
        self._attr_unique_id = f"{cu_mac}_{device.id}"
        # The entity name must match the HomeKit `accessory.name` format
        # `name + ' ' + zone` (verified against homebridge-switchbee
        # `homekit/Switch.js:20`) so the migration adoption (P6) preserves
        # the original_name byte-for-byte from the homekit_controller row.
        # See plan Phase 5b decision / line 1085. Zone may be empty for
        # items whose CU configuration has no room assignment.
        if device.zone:
            self._attr_name = f"{device.name} {device.zone}"
        else:
            self._attr_name = device.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cu_mac)},
            name="SwitchBee Central Unit",
            manufacturer="SwitchBee",
        )

    @property
    def available(self) -> bool:
        """Return False when WS is down OR the cached state is OFFLINE."""
        if not getattr(self.coordinator.client, "connected", True):
            return False
        raw = self.coordinator.data.get(self._device.id)
        if raw in _OFFLINE_SENTINELS:
            return False
        return super().available

    async def async_added_to_hass(self) -> None:
        """Subscribe to the per-item dispatcher signal."""
        await super().async_added_to_hass()
        signal = self.coordinator.signal_for(self._device.id)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal, self._handle_push_value
            )
        )

    def _handle_push_value(self, value: object) -> None:
        """Receive a push value from the dispatcher and refresh state.

        Subclasses may override this to do their own translation, but the
        default behaviour of re-rendering against `coordinator.data` is
        usually enough because the coordinator already updated its cache
        before firing the signal.
        """
        del value  # already mirrored into coordinator.data
        # Only write state when the entity is fully attached to a HA
        # platform. Unit tests that exercise just the subscription path
        # do not run the platform's `add_to_platform_finish` step;
        # skipping the write there keeps the dispatcher contract honest
        # without tripping HA's thread-safety / unattached-entity guards.
        if getattr(self, "_platform_state", None) is not EntityPlatformState.ADDED:
            return
        self.async_write_ha_state()


__all__ = ["SwitchBeeEntity"]
