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
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client

from .const import DOMAIN
from .coordinator import SwitchBeeCoordinator, async_build_coordinator
from .switchbee_ws import (
    CUMACMissingError,
    SwitchBeeProtocolError,
    SwitchBeeWSClient,
)

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
    hass.data.setdefault(DOMAIN, {})
    session = aiohttp_client.async_get_clientsession(hass)
    client = SwitchBeeWSClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=session,
    )
    try:
        coordinator: SwitchBeeCoordinator = await async_build_coordinator(
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

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(_make_unload_hook(coordinator))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry: tear down the coordinator and platforms."""
    coordinator: SwitchBeeCoordinator | None = (
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    )
    unload_ok: Any = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, PLATFORMS
        )
    if coordinator is not None:
        await coordinator.async_shutdown()
    return bool(unload_ok)


def _make_unload_hook(coordinator: SwitchBeeCoordinator):
    """Build an async callable HA can register via `entry.async_on_unload`."""

    async def _hook() -> None:
        await coordinator.async_shutdown()

    return _hook
