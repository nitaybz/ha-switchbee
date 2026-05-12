"""ConfigFlow + OptionsFlow for the SwitchBee Local integration.

The user step (`async_step_user`) asks for host, username, and password,
then validates the credentials by opening a short-lived
`SwitchBeeWSClient`, calling `start()` (which performs LOGIN), and calling
`get_configuration()` to learn the CU MAC. The MAC is the unique_id of the
resulting ConfigEntry (Decision #15) and the entry title is the literal
string `"SwitchBee {cu_mac}"` (Phase 3 plan locks this format).

The voluptuous schemas (`USER_DATA_SCHEMA`, `OPTIONS_SCHEMA`) are exported
as module-level constants so they can be unit-tested without an HA test
harness; see `tests/test_config_flow_schema.py`.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client

from .const import DEFAULT_PORT, DOMAIN
from .switchbee_ws import (
    CUMACMissingError,
    SwitchBeeProtocolError,
    SwitchBeeWSClient,
    normalize_cu_mac,
)

_LOGGER = logging.getLogger(__name__)

# Option key for the connection timeout tunable (seconds).
CONF_CONNECTION_TIMEOUT = "connection_timeout"
DEFAULT_CONNECTION_TIMEOUT: int = 5


def _non_empty_string(value: Any) -> str:
    """Voluptuous validator: must be a non-empty string after stripping."""
    if not isinstance(value, str) or not value.strip():
        raise vol.Invalid("must be a non-empty string")
    return value


def _positive_int(value: Any) -> int:
    """Voluptuous validator: must be a strictly positive int (no booleans)."""
    if isinstance(value, bool):
        raise vol.Invalid("must be an integer, not a bool")
    if not isinstance(value, int):
        raise vol.Invalid("must be an integer")
    if value <= 0:
        raise vol.Invalid("must be > 0")
    return value


# Schema for the initial `user` step. All three fields are required and
# non-empty; the password is not echoed back so the form does not expose it
# to retries.
USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): _non_empty_string,
        vol.Required(CONF_USERNAME): _non_empty_string,
        vol.Required(CONF_PASSWORD): _non_empty_string,
    }
)

# Schema for the OptionsFlow. Currently a single tunable: connection timeout
# in seconds. Default is `DEFAULT_CONNECTION_TIMEOUT` (5s) to match the
# protocol module's LOGIN timeout.
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONF_CONNECTION_TIMEOUT,
            default=DEFAULT_CONNECTION_TIMEOUT,
        ): _positive_int,
    }
)


class CannotConnectError(Exception):
    """Validation error: could not reach or LOGIN to the CU."""


class InvalidAuthError(Exception):
    """Validation error: the CU rejected the credentials."""


async def _validate_user_input(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """Run a short-lived LOGIN against the CU and return its MAC.

    Raises:
        CannotConnectError: WS unreachable, LOGIN timed out, or the CU did
            not return a usable `mac` field.
        InvalidAuthError: the CU rejected the username / password.
    """
    session = aiohttp_client.async_get_clientsession(hass)
    client = SwitchBeeWSClient(
        host=data[CONF_HOST],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        session=session,
    )
    try:
        await client.start()
        raw_config = await client.get_configuration()
    except CUMACMissingError as err:
        raise CannotConnectError(str(err)) from err
    except SwitchBeeProtocolError as err:
        # The protocol module wraps INVALID_CREDENTIALS in a generic
        # SwitchBeeProtocolError; distinguish by message substring.
        if "INVALID" in str(err).upper() and "CRED" in str(err).upper():
            raise InvalidAuthError(str(err)) from err
        raise CannotConnectError(str(err)) from err
    except (OSError, TimeoutError) as err:
        raise CannotConnectError(str(err)) from err
    finally:
        # Always stop the validation client; the runtime coordinator uses
        # a fresh one (Phase 3 risk note: validation race with reconnect).
        try:
            await client.stop()
        except Exception:  # pragma: no cover - best-effort cleanup
            _LOGGER.debug("Validation client stop raised; ignoring")

    mac_raw = raw_config.get("mac")
    return normalize_cu_mac(mac_raw)


class SwitchBeeConfigFlow(ConfigFlow, domain=DOMAIN):
    """User-facing config flow for SwitchBee Local."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Initial step: collect host, username, password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                validated = USER_DATA_SCHEMA(user_input)
            except vol.Invalid:
                errors["base"] = "unknown"
            else:
                try:
                    cu_mac = await _validate_user_input(self.hass, validated)
                except InvalidAuthError:
                    errors["base"] = "invalid_auth"
                except CUMACMissingError:
                    errors["base"] = "cannot_connect"
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001 - last resort
                    _LOGGER.exception("Unexpected error validating SwitchBee CU")
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(cu_mac)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"SwitchBee {cu_mac}",
                        data={
                            CONF_HOST: validated[CONF_HOST],
                            CONF_USERNAME: validated[CONF_USERNAME],
                            CONF_PASSWORD: validated[CONF_PASSWORD],
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> SwitchBeeOptionsFlow:
        """Return the OptionsFlow handler."""
        return SwitchBeeOptionsFlow(config_entry)


class SwitchBeeOptionsFlow(OptionsFlow):
    """Options flow: one tunable for now (connection timeout)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # HA 2024.11+ deprecated assigning to `self.config_entry`; the
        # framework injects it via the constructor. Be tolerant of either
        # convention so the integration loads on the current PHCC pin.
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options.get(CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT)
        schema = vol.Schema(
            {
                vol.Optional(CONF_CONNECTION_TIMEOUT, default=current): _positive_int,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


__all__ = [
    "CONF_CONNECTION_TIMEOUT",
    "DEFAULT_CONNECTION_TIMEOUT",
    "OPTIONS_SCHEMA",
    "USER_DATA_SCHEMA",
    "CannotConnectError",
    "InvalidAuthError",
    "SwitchBeeConfigFlow",
    "SwitchBeeOptionsFlow",
]

# Keep a reference to the imported `config_entries` module so flake8 does
# not flag it (HA's older lint stripped this; ruff is fine).
_ = config_entries
# Domain constant of the module: HA introspects this via the metaclass
# when `domain=DOMAIN` is supplied above. Re-asserting it here for clarity.
assert SwitchBeeConfigFlow.__init_subclass__  # noqa: S101
# DEFAULT_PORT is exposed by const.py; bind for tests that read it via the
# `SwitchBeeWSClient` defaults.
_ = DEFAULT_PORT
