"""HA-shaped integration tests for `SwitchBeeConfigFlow`.

These tests require `pytest-homeassistant-custom-component` (added to
`pyproject.toml` dev deps in Phase 3) because they exercise the real
`hass.config_entries.flow.async_init` machinery and assert against the
resulting `FlowResult` payloads.

The WS client is patched so the tests do not need a real CU or a fake CU
socket server. Pure-Python protocol round-trips already have coverage in
`test_switchbee_ws.py`; this file only proves the HA glue is wired
correctly (user step -> validation -> create_entry, with unique_id and
title set per the plan).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha_switchbee.const import DOMAIN

# Verified-live MAC from Moshe's CU (also used by tests/fake_cu.py).
SAMPLE_MAC_RAW = "A8-21-08-E7-68-8F"
SAMPLE_MAC_NORMALIZED = "a82108e7688f"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Make HA load `custom_components/ha_switchbee` during tests."""
    yield


class _PatchedClient:
    """Multi-target patch of SwitchBeeWSClient for both config_flow and __init__.

    The config_flow uses a short-lived validation client; once the entry is
    created HA's setup machinery calls `async_setup_entry` in __init__.py
    which constructs a long-lived runtime client. Both must be replaced
    with a benign mock during tests so HA does not actually try to open
    a TCP connection to the host string in the test fixture.
    """

    def __init__(self, *, login_ok: bool = True, has_mac: bool = True) -> None:
        self._login_ok = login_ok
        self._has_mac = has_mac
        self._patches: list = []

    def _build_instance(self) -> AsyncMock:
        instance = AsyncMock()
        if self._login_ok:
            instance.start = AsyncMock(return_value=None)
            instance.get_configuration = AsyncMock(
                return_value=(
                    {"mac": SAMPLE_MAC_RAW, "zones": []} if self._has_mac else {"zones": []}
                )
            )
        else:
            from custom_components.ha_switchbee.switchbee_ws import (
                SwitchBeeProtocolError,
            )

            instance.start = AsyncMock(
                side_effect=SwitchBeeProtocolError("CU rejected 'LOGIN': INVALID_CREDENTIALS")
            )
            instance.get_configuration = AsyncMock(
                return_value={"mac": SAMPLE_MAC_RAW, "zones": []}
            )
        instance.stop = AsyncMock(return_value=None)
        # `connected` is read by the coordinator's shutdown path.
        instance.connected = False
        # `add_listener` returns an unsubscribe callable.
        instance.add_listener = lambda cb: lambda: None
        return instance

    def __enter__(self):
        # Each constructor call returns a fresh mock instance.
        for target in (
            "custom_components.ha_switchbee.config_flow.SwitchBeeWSClient",
            "custom_components.ha_switchbee.SwitchBeeWSClient",
        ):
            p = patch(target, side_effect=lambda *a, **kw: self._build_instance())
            self._patches.append(p)
            p.start()
        return self

    def __exit__(self, *_exc):
        while self._patches:
            self._patches.pop().stop()


def _patched_client(*, login_ok: bool = True, has_mac: bool = True) -> _PatchedClient:
    return _PatchedClient(login_ok=login_ok, has_mac=has_mac)


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    """Submitting no input shows the user form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] in (None, {})


async def test_user_step_creates_entry_on_success(hass: HomeAssistant) -> None:
    """Valid creds -> entry created with normalized cu_mac as unique_id."""
    with _patched_client():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] == FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.68.57",
                CONF_USERNAME: "user",
                CONF_PASSWORD: "secret",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == f"SwitchBee {SAMPLE_MAC_NORMALIZED}"
    entry = result["result"]
    assert entry.unique_id == SAMPLE_MAC_NORMALIZED
    assert entry.data[CONF_HOST] == "192.168.68.57"
    assert entry.data[CONF_USERNAME] == "user"
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_user_step_rejects_duplicate(hass: HomeAssistant) -> None:
    """A second add of the same CU MAC aborts with already_configured."""
    with _patched_client():
        first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        first = await hass.config_entries.flow.async_configure(
            first["flow_id"],
            {
                CONF_HOST: "192.168.68.57",
                CONF_USERNAME: "user",
                CONF_PASSWORD: "secret",
            },
        )
        await hass.async_block_till_done()
        assert first["type"] == FlowResultType.CREATE_ENTRY

        # Same CU, second add.
        second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"],
            {
                CONF_HOST: "192.168.68.57",
                CONF_USERNAME: "user",
                CONF_PASSWORD: "secret",
            },
        )
        await hass.async_block_till_done()

    assert second["type"] == FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_user_step_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    """Bad creds -> form re-shown with invalid_auth error."""
    with _patched_client(login_ok=False):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.68.57",
                CONF_USERNAME: "user",
                CONF_PASSWORD: "wrong",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_missing_mac_is_cannot_connect(
    hass: HomeAssistant,
) -> None:
    """CU that returns no mac -> cannot_connect error (Decision #15)."""
    with _patched_client(has_mac=False):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.68.57",
                CONF_USERNAME: "user",
                CONF_PASSWORD: "secret",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow_sets_connection_timeout(hass: HomeAssistant) -> None:
    """OptionsFlow accepts the connection_timeout tunable."""
    from custom_components.ha_switchbee.config_flow import (
        CONF_CONNECTION_TIMEOUT,
    )

    with _patched_client():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.68.57",
                CONF_USERNAME: "user",
                CONF_PASSWORD: "secret",
            },
        )
        await hass.async_block_till_done()

    entry = result["result"]

    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] == FlowResultType.FORM
    assert options["step_id"] == "init"

    options = await hass.config_entries.options.async_configure(
        options["flow_id"], {CONF_CONNECTION_TIMEOUT: 12}
    )
    await hass.async_block_till_done()
    assert options["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options.get(CONF_CONNECTION_TIMEOUT) == 12
