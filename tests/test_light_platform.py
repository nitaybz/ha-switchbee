"""Unit tests for the SwitchBee `light` platform (DIMMER only)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_switchbee.const import DOMAIN
from custom_components.ha_switchbee.light import (
    SwitchBeeLight,
    async_setup_entry,
)
from custom_components.ha_switchbee.models import SwitchBeeDevice


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


def _light_device(item_id: int, state: int = 0) -> SwitchBeeDevice:
    return SwitchBeeDevice(
        id=item_id,
        name=f"Dimmer {item_id}",
        hw="hw",
        type="DIMMER",
        zone="Zone",
        state=state,
    )


def test_async_setup_entry_filters_only_dimmer() -> None:
    """Only DIMMER items become light entities."""
    devices = {
        1: _light_device(1, 0),
        2: SwitchBeeDevice(id=2, name="Switch", hw="hw", type="SWITCH", zone="Zone", state="OFF"),
        3: SwitchBeeDevice(id=3, name="Shutter", hw="hw", type="SHUTTER", zone="Zone", state=0),
    }
    coordinator = _StubCoordinator(devices=devices)
    collected: list[SwitchBeeLight] = []

    class _FakeEntry:
        entry_id = "test_entry"

    fake_hass = type("FakeHass", (), {"data": {DOMAIN: {"test_entry": coordinator}}})()

    def _add(entities, update_before_add=False) -> None:
        collected.extend(entities)

    import asyncio

    asyncio.run(async_setup_entry(fake_hass, _FakeEntry(), _add))
    assert {e._device.id for e in collected} == {1}


@pytest.mark.parametrize(
    ("ha_brightness", "cu_percent"),
    [
        (255, 100),
        (128, 50),
        (1, 0),  # 1/255 -> rounds to 0%
        (3, 1),  # 3/255 -> rounds to 1%
    ],
)
async def test_light_turn_on_scales_ha_brightness_to_cu_percent(
    hass: HomeAssistant,
    ha_brightness: int,
    cu_percent: int,
) -> None:
    """HA brightness (0-255) maps to CU percent (0-100)."""
    dev = _light_device(7, state=0)
    coord = _StubCoordinator(devices={7: dev})
    ent = SwitchBeeLight(coord, dev)
    await ent.async_turn_on(brightness=ha_brightness)
    assert coord.client.operate_calls == [(7, cu_percent)]


async def test_light_turn_on_no_brightness_uses_full(
    hass: HomeAssistant,
) -> None:
    """`light.turn_on` with no brightness sends 100 (full on)."""
    dev = _light_device(7, state=0)
    coord = _StubCoordinator(devices={7: dev})
    ent = SwitchBeeLight(coord, dev)
    await ent.async_turn_on()
    assert coord.client.operate_calls == [(7, 100)]


async def test_light_turn_off_sends_zero(hass: HomeAssistant) -> None:
    """`light.turn_off` sends OPERATE(item_id, 0)."""
    dev = _light_device(7, state=80)
    coord = _StubCoordinator(devices={7: dev})
    ent = SwitchBeeLight(coord, dev)
    await ent.async_turn_off()
    assert coord.client.operate_calls == [(7, 0)]


@pytest.mark.parametrize(
    ("cu_state", "expected_is_on", "expected_brightness"),
    [
        (0, False, 0),
        (50, True, 128),  # 50% -> 128/255 (rounded)
        (100, True, 255),
        (75, True, 191),  # 75% -> 191/255
    ],
)
async def test_light_state_maps_to_is_on_and_brightness(
    hass: HomeAssistant,
    cu_state: int,
    expected_is_on: bool,
    expected_brightness: int,
) -> None:
    """CU percent (0-100) maps to is_on + HA brightness (0-255)."""
    dev = _light_device(7, state=cu_state)
    coord = _StubCoordinator(devices={7: dev})
    coord.data[7] = cu_state
    ent = SwitchBeeLight(coord, dev)
    assert ent.is_on is expected_is_on
    assert ent.brightness == expected_brightness


async def test_light_offline_state_makes_unavailable(
    hass: HomeAssistant,
) -> None:
    """OFFLINE sentinel makes the light unavailable, not on at 0%."""
    dev = _light_device(7)
    coord = _StubCoordinator(devices={7: dev})
    coord.data[7] = -1
    ent = SwitchBeeLight(coord, dev)
    assert ent.available is False


async def test_light_color_mode_is_brightness_only(
    hass: HomeAssistant,
) -> None:
    """SwitchBee dimmers are brightness-only; expose ColorMode.BRIGHTNESS."""
    from homeassistant.components.light import ColorMode

    dev = _light_device(7)
    coord = _StubCoordinator(devices={7: dev})
    ent = SwitchBeeLight(coord, dev)
    assert ent.color_mode == ColorMode.BRIGHTNESS
    assert ColorMode.BRIGHTNESS in ent.supported_color_modes
