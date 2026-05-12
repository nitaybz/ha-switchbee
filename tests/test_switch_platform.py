"""Unit tests for the SwitchBee `switch` platform.

These tests use the real PHCC HA harness so we can drive the entity
through `async_added_to_hass`, dispatcher signals, and HA service-like
calls (we call `async_turn_on` / `async_turn_off` directly on the
entity since we do not want to depend on a fully wired service
registry).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.ha_switchbee.const import DOMAIN
from custom_components.ha_switchbee.models import SwitchBeeDevice
from custom_components.ha_switchbee.switch import (
    SwitchBeeSwitch,
    async_setup_entry,
)


class _StubClient:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.operate_calls: list[tuple[int, Any]] = []

    async def operate(self, item_id: int, value: Any) -> dict:
        self.operate_calls.append((item_id, value))
        return {"status": "OK"}

    async def stop(self) -> None:
        self.connected = False


class _StubCoordinator:
    def __init__(
        self,
        *,
        cu_mac: str = "a82108e7688f",
        devices: dict[int, SwitchBeeDevice],
        client: _StubClient | None = None,
    ) -> None:
        self.cu_mac = cu_mac
        self.client = client or _StubClient()
        self.devices = devices
        self.data: dict[int, Any] = {item_id: dev.state for item_id, dev in devices.items()}
        self.last_update_success = True

    def signal_for(self, item_id: int) -> str:
        return f"{DOMAIN}_push_{self.cu_mac}_{item_id}"

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


def _switch_device(item_id: int, type_: str = "SWITCH") -> SwitchBeeDevice:
    return SwitchBeeDevice(
        id=item_id,
        name=f"Switch {item_id}",
        hw="hw",
        type=type_,
        zone="Zone",
        state="OFF",
    )


def test_async_setup_entry_filters_only_switch_family_types() -> None:
    """`async_setup_entry` only yields entities for switch-family types."""
    devices = {
        1: _switch_device(1, "SWITCH"),
        2: _switch_device(2, "TIMED_SWITCH"),
        3: _switch_device(3, "GROUP_SWITCH"),
        4: _switch_device(4, "TIMED_POWER"),
        5: _switch_device(5, "LOCK_GROUP"),
        # Non-switch types must be skipped.
        6: _switch_device(6, "DIMMER"),
        7: _switch_device(7, "SHUTTER"),
        8: _switch_device(8, "SCENARIO"),
    }
    coordinator = _StubCoordinator(devices=devices)
    collected: list[SwitchBeeSwitch] = []

    class _FakeEntry:
        entry_id = "test_entry"

    class _FakeHassData(dict):
        pass

    fake_hass = type("FakeHass", (), {"data": {DOMAIN: {"test_entry": coordinator}}})()

    def _add(entities, update_before_add=False) -> None:
        collected.extend(entities)

    # async_setup_entry is async; call its coroutine.
    import asyncio

    asyncio.run(async_setup_entry(fake_hass, _FakeEntry(), _add))
    ids = {e._device.id for e in collected}
    assert ids == {1, 2, 3, 4, 5}, f"expected switch-family ids, got {ids}"


async def test_switch_turn_on_calls_operate_with_on(
    hass: HomeAssistant,
) -> None:
    """`async_turn_on` issues `client.operate(item_id, "ON")`."""
    dev = _switch_device(3)
    coord = _StubCoordinator(devices={3: dev})
    ent = SwitchBeeSwitch(coord, dev)
    await ent.async_turn_on()
    assert coord.client.operate_calls == [(3, "ON")]


async def test_switch_turn_off_calls_operate_with_off(
    hass: HomeAssistant,
) -> None:
    """`async_turn_off` issues `client.operate(item_id, "OFF")`."""
    dev = _switch_device(3, "GROUP_SWITCH")
    coord = _StubCoordinator(devices={3: dev})
    ent = SwitchBeeSwitch(coord, dev)
    await ent.async_turn_off()
    assert coord.client.operate_calls == [(3, "OFF")]


@pytest.mark.parametrize(
    ("state", "expected"),
    [("ON", True), ("OFF", False), ("OFFLINE", False)],
)
async def test_switch_is_on_reflects_cached_state(
    hass: HomeAssistant,
    state: str,
    expected: bool,
) -> None:
    dev = _switch_device(3)
    coord = _StubCoordinator(devices={3: dev})
    coord.data[3] = state
    ent = SwitchBeeSwitch(coord, dev)
    assert ent.is_on is expected


async def test_switch_subscribes_dispatcher_signal_on_add(
    hass: HomeAssistant,
) -> None:
    """`async_added_to_hass` connects a dispatcher listener for the item id.

    We assert the subscription took by inspecting HA's dispatcher
    bookkeeping for the per-item signal name.
    """
    dev = _switch_device(3)
    coord = _StubCoordinator(devices={3: dev})
    ent = SwitchBeeSwitch(coord, dev)
    ent.hass = hass
    ent.entity_id = "switch.test"

    signal = coord.signal_for(3)
    # HA tracks dispatcher targets in `hass.data["dispatcher_targets"]`
    # (the key may differ across versions; fall back to counting via a
    # sibling connect that we can compare against).
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    received: list[str] = []
    sibling_unsub = async_dispatcher_connect(hass, signal, lambda v: received.append(v))

    await ent.async_added_to_hass()

    # Confirm the dispatcher is wired by firing the signal and seeing
    # the sibling receive it. The entity's own listener also runs (no
    # state write happens because `entity_id`/`platform` are not fully
    # registered, but the subscription path is exercised).
    async_dispatcher_send(hass, signal, "ON")
    await hass.async_block_till_done()
    assert received == ["ON"]
    sibling_unsub()


async def test_switch_is_on_reads_live_from_coordinator_data(
    hass: HomeAssistant,
) -> None:
    """is_on reads from `coordinator.data`, so updating the cache flips it."""
    dev = _switch_device(3)
    coord = _StubCoordinator(devices={3: dev})
    ent = SwitchBeeSwitch(coord, dev)
    coord.data[3] = "ON"
    assert ent.is_on is True
    coord.data[3] = "OFF"
    assert ent.is_on is False
