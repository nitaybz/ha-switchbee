"""Unit tests for the SwitchBee `scene` platform.

SCENARIO and ROLLING_SCENARIO items become HA `Scene` entities. Per the
JS source's unified state mapping, scene activation is the same OPERATE
payload as a switch-on: `OPERATE(itemId, "ON")`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.ha_switchbee.const import DOMAIN
from custom_components.ha_switchbee.models import SwitchBeeDevice
from custom_components.ha_switchbee.scene import (
    SwitchBeeScene,
    async_setup_entry,
)


class _StubClient:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.operate_calls: list[tuple[int, Any]] = []

    async def operate(self, item_id: int, value: Any) -> dict:
        self.operate_calls.append((item_id, value))
        return {"status": "OK"}


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
        self.data: dict[int, Any] = {i: d.state for i, d in devices.items()}
        self.last_update_success = True

    def signal_for(self, item_id: int) -> str:
        return f"{DOMAIN}_push_{self.cu_mac}_{item_id}"

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


def _scene_device(item_id: int, type_: str = "SCENARIO") -> SwitchBeeDevice:
    return SwitchBeeDevice(
        id=item_id,
        name=f"Scene {item_id}",
        hw="hw",
        type=type_,
        zone="Zone",
        state="OFF",
    )


def test_async_setup_entry_filters_scenario_and_rolling_scenario() -> None:
    """SCENARIO and ROLLING_SCENARIO both yield scene entities."""
    devices = {
        1: _scene_device(1, "SCENARIO"),
        2: _scene_device(2, "ROLLING_SCENARIO"),
        # Filtered out:
        3: SwitchBeeDevice(id=3, name="S", hw="h", type="SWITCH", zone="Z", state="OFF"),
    }
    coordinator = _StubCoordinator(devices=devices)
    collected: list[SwitchBeeScene] = []

    class _FakeEntry:
        entry_id = "test_entry"

    fake_hass = type("FakeHass", (), {"data": {DOMAIN: {"test_entry": coordinator}}})()

    def _add(entities, update_before_add=False) -> None:
        collected.extend(entities)

    import asyncio

    asyncio.run(async_setup_entry(fake_hass, _FakeEntry(), _add))
    assert {e._device.id for e in collected} == {1, 2}


async def test_scene_activate_sends_on(hass: HomeAssistant) -> None:
    """SCENARIO `async_activate` triggers OPERATE(item_id, "ON")."""
    dev = _scene_device(50, "SCENARIO")
    coord = _StubCoordinator(devices={50: dev})
    ent = SwitchBeeScene(coord, dev)
    await ent.async_activate()
    assert coord.client.operate_calls == [(50, "ON")]


async def test_rolling_scenario_activate_sends_on(
    hass: HomeAssistant,
) -> None:
    """ROLLING_SCENARIO `async_activate` also triggers OPERATE(id, "ON")."""
    dev = _scene_device(51, "ROLLING_SCENARIO")
    coord = _StubCoordinator(devices={51: dev})
    ent = SwitchBeeScene(coord, dev)
    await ent.async_activate()
    assert coord.client.operate_calls == [(51, "ON")]


async def test_scene_unique_id_uses_cu_mac_prefix(
    hass: HomeAssistant,
) -> None:
    """Scenes get the same unique_id format as every other entity."""
    dev = _scene_device(50, "SCENARIO")
    coord = _StubCoordinator(devices={50: dev})
    ent = SwitchBeeScene(coord, dev)
    assert ent.unique_id == "a82108e7688f_50"
