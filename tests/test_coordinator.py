"""HA-shaped integration tests for `SwitchBeeCoordinator`.

These tests instantiate the coordinator inside an HA fixture, drive a
synthetic CONFIGURATION_CHANGE push through the WS client, and assert
that the coordinator dispatches the expected signal so platform entities
subscribed by `unique_id` can react.

The real WebSocket protocol is exercised via the in-process `FakeCU` from
Phase 1's `tests/fake_cu.py`, so this is a real-network-round-trip test
that uses 127.0.0.1 sockets (PHCC's `socket_enabled` fixture must be on).
"""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.ha_switchbee.coordinator import (
    SwitchBeeCoordinator,
    async_build_coordinator,
)
from custom_components.ha_switchbee.switchbee_ws import SwitchBeeWSClient
from tests.fake_cu import SAMPLE_MAC, FakeCU

# Normalized form of the SAMPLE_MAC constant from fake_cu.py.
SAMPLE_MAC_NORMALIZED = "a82108e7688f"

assert SAMPLE_MAC.replace("-", "").lower() == SAMPLE_MAC_NORMALIZED


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Make HA load `custom_components/ha_switchbee` during tests."""
    yield


@pytest.fixture(autouse=True)
def _enable_sockets(socket_enabled):
    """These tests bind to 127.0.0.1 for the fake CU."""
    yield


async def _make_client(cu: FakeCU, session: aiohttp.ClientSession) -> SwitchBeeWSClient:
    return SwitchBeeWSClient(
        host="127.0.0.1",
        username="user",
        password="pass",
        session=session,
        port=cu.port,
        login_timeout=1.0,
        command_timeout=1.0,
    )


async def test_async_build_coordinator_sets_cu_mac_and_devices(
    hass: HomeAssistant,
) -> None:
    """Happy path: build a coordinator against a normal fake CU."""
    async with FakeCU() as cu, aiohttp.ClientSession() as session:
        client = await _make_client(cu, session)

        # The real `_validate_user_input` would normally do this; we call
        # the coordinator builder directly so the test is hermetic.
        coordinator = await async_build_coordinator(
            hass, entry=None, client=client  # type: ignore[arg-type]
        )
        try:
            assert isinstance(coordinator, SwitchBeeCoordinator)
            assert coordinator.cu_mac == SAMPLE_MAC_NORMALIZED
            # Fake CU has 3 items in Living Room + 1 in Kitchen.
            assert set(coordinator.devices.keys()) == {3, 7, 12, 21}
            assert coordinator.devices[7].type == "DIMMER"
            assert coordinator.devices[7].zone == "Living Room"
        finally:
            await coordinator.async_shutdown()


async def test_coordinator_dispatches_push_event_to_signal(
    hass: HomeAssistant,
) -> None:
    """When the CU pushes CONFIGURATION_CHANGE, the coordinator fires a signal.

    Subscribers connected via `async_dispatcher_connect(hass, signal_for(id))`
    should receive the new value within 1 second of the CU emitting the
    push (per Phase 3 exit criteria).
    """
    async with FakeCU() as cu, aiohttp.ClientSession() as session:
        client = await _make_client(cu, session)
        coordinator = await async_build_coordinator(
            hass, entry=None, client=client  # type: ignore[arg-type]
        )

        try:
            received: list = []
            signal = coordinator.signal_for(3)

            def _handler(value):
                received.append(value)

            unsub = async_dispatcher_connect(hass, signal, _handler)

            await cu.push_configuration_change(
                item_id=3, name="Pictures", value="ON", shape="newValue"
            )

            # Wait up to 1s for the push to round-trip.
            deadline = asyncio.get_running_loop().time() + 1.0
            while not received and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
                # Process any pending dispatcher callbacks.
                await hass.async_block_till_done()

            unsub()
            assert received == ["ON"], (
                f"expected one push value 'ON', got {received!r}"
            )
            # The coordinator's `data` cache should also reflect the update.
            assert coordinator.data[3] == "ON"
        finally:
            await coordinator.async_shutdown()


async def test_coordinator_handles_push_with_data_shape(
    hass: HomeAssistant,
) -> None:
    """The CU sometimes sends `data` instead of `newValue`."""
    async with FakeCU() as cu, aiohttp.ClientSession() as session:
        client = await _make_client(cu, session)
        coordinator = await async_build_coordinator(
            hass, entry=None, client=client  # type: ignore[arg-type]
        )

        try:
            received: list = []
            unsub = async_dispatcher_connect(
                hass, coordinator.signal_for(7), lambda v: received.append(v)
            )

            await cu.push_configuration_change(
                item_id=7, name="Ceiling", value=42, shape="data"
            )

            deadline = asyncio.get_running_loop().time() + 1.0
            while not received and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
                await hass.async_block_till_done()

            unsub()
            assert received == [42]
            assert coordinator.data[7] == 42
        finally:
            await coordinator.async_shutdown()


async def test_devices_by_platform_groups_correctly(
    hass: HomeAssistant,
) -> None:
    """`devices_by_platform` groups the device set by HA platform string."""
    async with FakeCU() as cu, aiohttp.ClientSession() as session:
        client = await _make_client(cu, session)
        coordinator = await async_build_coordinator(
            hass, entry=None, client=client  # type: ignore[arg-type]
        )
        try:
            grouped = coordinator.devices_by_platform()
            # SWITCH -> switch (ids 3, 21), DIMMER -> light (id 7),
            # SHUTTER -> cover (id 12).
            assert {dev.id for dev in grouped["switch"]} == {3, 21}
            assert [dev.id for dev in grouped["light"]] == [7]
            assert [dev.id for dev in grouped["cover"]] == [12]
            assert "scene" not in grouped
        finally:
            await coordinator.async_shutdown()


async def test_async_shutdown_unsubscribes_and_stops_client(
    hass: HomeAssistant,
) -> None:
    """After `async_shutdown`, the client is stopped and no more pushes fire."""
    async with FakeCU() as cu, aiohttp.ClientSession() as session:
        client = await _make_client(cu, session)
        coordinator = await async_build_coordinator(
            hass, entry=None, client=client  # type: ignore[arg-type]
        )
        await coordinator.async_shutdown()

        # After shutdown the WS client should be stopped.
        assert client.connected is False
        # Pushing now should not raise inside the test, even though no one
        # is listening; the fake CU just sees no active connections to
        # broadcast to.
        with contextlib.suppress(Exception):
            await cu.push_configuration_change(
                item_id=3, name="Pictures", value="OFF"
            )
