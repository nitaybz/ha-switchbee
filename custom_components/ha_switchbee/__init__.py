"""SwitchBee Local custom integration entry points.

`async_setup_entry` wires the long-lived `SwitchBeeWSClient`, fetches the
initial GET_CONFIGURATION, builds a `SwitchBeeCoordinator`, stashes it in
`hass.data[DOMAIN][entry.entry_id]`, and forwards setup to every shipped
platform listed in `PLATFORMS`. Phase 3 ships the coordinator and the
config flow; Phase 4 will add `switch`, `light`, `cover`, and `scene` to
PLATFORMS.

Note on imports: every `homeassistant.*` symbol used here lives in the
runtime path because HA loads this module from inside its own process.
There are no `TYPE_CHECKING` guards because Phase 3 needs the real
runtime types, not just type hints.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .const import DOMAIN
from .switchbee_ws import (
    CUMACMissingError,
    SwitchBeeProtocolError,
    SwitchBeeWSClient,
)

# Home Assistant imports are deferred to call-time so that tools that only
# need the protocol module (e.g. tools/probe.py and tools/migrate.py) can
# import `custom_components.ha_switchbee.switchbee_ws` on operator boxes
# that do not have Home Assistant installed in their Python env. Inside HA
# itself these imports are free (HA is the importer), so there is no
# runtime cost. Test code patches these names against this module so we
# re-export them; the protocol module itself has zero HA dependency.
if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Platforms shipped by Phase 4. The four v1 platforms are switch, light,
# cover, and scene. Any future platform (binary_sensor, sensor) lands here
# after its Phase 4.x lift.
PLATFORMS: list[str] = ["switch", "light", "cover", "scene"]

__all__ = ["DOMAIN", "PLATFORMS", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SwitchBee Local from a config entry.

    Flow:
      1. Build a `SwitchBeeWSClient` from the entry's stored credentials.
      2. `await async_build_coordinator(...)` which calls
         `client.start()` (performs LOGIN) and `client.get_configuration()`,
         then normalizes the CU MAC.
      3. Stash the coordinator in `hass.data[DOMAIN][entry.entry_id]`.
      4. Forward setup to every platform in `PLATFORMS`.
      5. Register the shutdown hook on entry unload.

    Raises `ConfigEntryNotReady` if the CU is unreachable or LOGIN fails
    so HA's retry mechanism kicks in. `ConfigEntryAuthFailed` would be
    more precise for invalid creds, but the protocol module surfaces
    those as `SwitchBeeProtocolError` with `INVALID_CREDENTIALS` in the
    message; mapping that distinction is a Phase 4 polish.
    """
    # Deferred imports: HA is guaranteed available at runtime because HA
    # is the importer; doing them here keeps the package importable on
    # boxes that do not have HA installed (operator tooling, CI).
    from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
    from homeassistant.exceptions import ConfigEntryNotReady
    from homeassistant.helpers import aiohttp_client

    from .coordinator import async_build_coordinator

    hass.data.setdefault(DOMAIN, {})
    session = aiohttp_client.async_get_clientsession(hass)
    client = SwitchBeeWSClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=session,
    )
    try:
        coordinator = await async_build_coordinator(
            hass, entry, client
        )
    except CUMACMissingError as err:
        raise ConfigEntryNotReady(
            f"SwitchBee CU did not return a usable MAC: {err}"
        ) from err
    except SwitchBeeProtocolError as err:
        raise ConfigEntryNotReady(
            f"SwitchBee LOGIN/GET_CONFIGURATION failed: {err}"
        ) from err
    except (OSError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            f"SwitchBee CU unreachable: {err}"
        ) from err

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register the SwitchBee Central Unit "bridge" device. Every per-item
    # entity device targets this one as its `via_device`, so HA shows the
    # full hierarchy (CU at the top, each switch/light/cover hanging off
    # it). The CU device has no entities of its own; it exists as the
    # grouping anchor.
    from homeassistant.helpers import device_registry as dr  # lazy: HA only at runtime

    device_reg = dr.async_get(hass)
    device_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator.cu_mac)},
        manufacturer="SwitchBee",
        name="SwitchBee Central Unit",
        model="Central Unit",
    )

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(_make_unload_hook(coordinator))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry: tear down the coordinator and platforms."""
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    unload_ok: Any = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, PLATFORMS
        )
    if coordinator is not None:
        await coordinator.async_shutdown()
    return bool(unload_ok)


def _make_unload_hook(coordinator):
    """Build an async callable HA can register via `entry.async_on_unload`."""

    async def _hook() -> None:
        await coordinator.async_shutdown()

    return _hook
