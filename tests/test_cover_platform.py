"""Unit tests for the SwitchBee `cover` platform.

Covers two flavours:
- SHUTTER / LOUVERED_SHUTTER: percent-based position (0-100).
- SOMFY: command-only (UP / DOWN / STOP); no position attribute.

The plan locks the public class name as `SwitchBeeCover`; internal
branching by `device.type` is acceptable.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_switchbee.const import DOMAIN
from custom_components.ha_switchbee.cover import (
    SwitchBeeCover,
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
        self.data: dict[int, Any] = {
            i: d.state for i, d in devices.items()
        }
        self.last_update_success = True

    def signal_for(self, item_id: int) -> str:
        return f"{DOMAIN}_push_{self.cu_mac}_{item_id}"

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


def _cover_device(
    item_id: int, type_: str = "SHUTTER", state: Any = 0,
) -> SwitchBeeDevice:
    return SwitchBeeDevice(
        id=item_id, name=f"Cover {item_id}", hw="hw", type=type_,
        zone="Zone", state=state,
    )


def test_async_setup_entry_filters_shutter_louvered_somfy() -> None:
    """SHUTTER, LOUVERED_SHUTTER, SOMFY all land on the cover platform."""
    devices = {
        1: _cover_device(1, "SHUTTER", state=50),
        2: _cover_device(2, "LOUVERED_SHUTTER", state=20),
        3: _cover_device(3, "SOMFY", state="STOP"),
        # Filtered out:
        4: SwitchBeeDevice(id=4, name="S", hw="h", type="SWITCH",
                           zone="Z", state="OFF"),
        5: SwitchBeeDevice(id=5, name="D", hw="h", type="DIMMER",
                           zone="Z", state=0),
    }
    coordinator = _StubCoordinator(devices=devices)
    collected: list[SwitchBeeCover] = []

    class _FakeEntry:
        entry_id = "test_entry"

    fake_hass = type(
        "FakeHass", (), {"data": {DOMAIN: {"test_entry": coordinator}}}
    )()

    def _add(entities, update_before_add=False) -> None:
        collected.extend(entities)

    import asyncio

    asyncio.run(async_setup_entry(fake_hass, _FakeEntry(), _add))
    assert {e._device.id for e in collected} == {1, 2, 3}


# ---------------------------------------------------------------------------
# SHUTTER (percent-based)
# ---------------------------------------------------------------------------


async def test_shutter_set_position_sends_percent(
    hass: HomeAssistant,
) -> None:
    """`set_cover_position position=60` -> OPERATE(item_id, 60)."""
    dev = _cover_device(12, "SHUTTER", state=0)
    coord = _StubCoordinator(devices={12: dev})
    ent = SwitchBeeCover(coord, dev)
    await ent.async_set_cover_position(position=60)
    assert coord.client.operate_calls == [(12, 60)]


async def test_shutter_open_sends_100(hass: HomeAssistant) -> None:
    """open_cover -> OPERATE(item_id, 100)."""
    dev = _cover_device(12, "SHUTTER", state=0)
    coord = _StubCoordinator(devices={12: dev})
    ent = SwitchBeeCover(coord, dev)
    await ent.async_open_cover()
    assert coord.client.operate_calls == [(12, 100)]


async def test_shutter_close_sends_0(hass: HomeAssistant) -> None:
    """close_cover -> OPERATE(item_id, 0)."""
    dev = _cover_device(12, "SHUTTER", state=80)
    coord = _StubCoordinator(devices={12: dev})
    ent = SwitchBeeCover(coord, dev)
    await ent.async_close_cover()
    assert coord.client.operate_calls == [(12, 0)]


async def test_shutter_stop_sends_stop_string(hass: HomeAssistant) -> None:
    """stop_cover -> OPERATE(item_id, "STOP")."""
    dev = _cover_device(12, "SHUTTER", state=40)
    coord = _StubCoordinator(devices={12: dev})
    ent = SwitchBeeCover(coord, dev)
    await ent.async_stop_cover()
    assert coord.client.operate_calls == [(12, "STOP")]


@pytest.mark.parametrize(
    ("raw", "expected_position", "expected_closed"),
    [
        (0, 0, True),
        (100, 100, False),
        (37, 37, False),
        (1, 1, False),
    ],
)
async def test_shutter_position_reads_from_cache(
    hass: HomeAssistant, raw: int, expected_position: int, expected_closed: bool,
) -> None:
    dev = _cover_device(12, "SHUTTER", state=raw)
    coord = _StubCoordinator(devices={12: dev})
    coord.data[12] = raw
    ent = SwitchBeeCover(coord, dev)
    assert ent.current_cover_position == expected_position
    assert ent.is_closed is expected_closed


async def test_louvered_shutter_uses_percent_semantics(
    hass: HomeAssistant,
) -> None:
    """LOUVERED_SHUTTER behaves like SHUTTER for position (tilt deferred)."""
    dev = _cover_device(15, "LOUVERED_SHUTTER", state=30)
    coord = _StubCoordinator(devices={15: dev})
    coord.data[15] = 30
    ent = SwitchBeeCover(coord, dev)
    assert ent.current_cover_position == 30
    await ent.async_set_cover_position(position=75)
    assert coord.client.operate_calls == [(15, 75)]


# ---------------------------------------------------------------------------
# SOMFY (command-based)
# ---------------------------------------------------------------------------


async def test_somfy_open_sends_up(hass: HomeAssistant) -> None:
    """SOMFY open_cover -> OPERATE(item_id, "UP")."""
    dev = _cover_device(20, "SOMFY", state="STOP")
    coord = _StubCoordinator(devices={20: dev})
    ent = SwitchBeeCover(coord, dev)
    await ent.async_open_cover()
    assert coord.client.operate_calls == [(20, "UP")]


async def test_somfy_close_sends_down(hass: HomeAssistant) -> None:
    """SOMFY close_cover -> OPERATE(item_id, "DOWN")."""
    dev = _cover_device(20, "SOMFY", state="STOP")
    coord = _StubCoordinator(devices={20: dev})
    ent = SwitchBeeCover(coord, dev)
    await ent.async_close_cover()
    assert coord.client.operate_calls == [(20, "DOWN")]


async def test_somfy_stop_sends_stop(hass: HomeAssistant) -> None:
    """SOMFY stop_cover -> OPERATE(item_id, "STOP")."""
    dev = _cover_device(20, "SOMFY", state="UP")
    coord = _StubCoordinator(devices={20: dev})
    ent = SwitchBeeCover(coord, dev)
    await ent.async_stop_cover()
    assert coord.client.operate_calls == [(20, "STOP")]


async def test_somfy_has_no_position_attribute(hass: HomeAssistant) -> None:
    """SOMFY items return None for `current_cover_position` (stateless)."""
    dev = _cover_device(20, "SOMFY", state="STOP")
    coord = _StubCoordinator(devices={20: dev})
    ent = SwitchBeeCover(coord, dev)
    assert ent.current_cover_position is None


async def test_somfy_supported_features_no_set_position(
    hass: HomeAssistant,
) -> None:
    """SOMFY exposes OPEN/CLOSE/STOP but NOT SET_POSITION."""
    from homeassistant.components.cover import CoverEntityFeature

    dev = _cover_device(20, "SOMFY", state="STOP")
    coord = _StubCoordinator(devices={20: dev})
    ent = SwitchBeeCover(coord, dev)
    features = ent.supported_features
    assert features & CoverEntityFeature.OPEN
    assert features & CoverEntityFeature.CLOSE
    assert features & CoverEntityFeature.STOP
    assert not (features & CoverEntityFeature.SET_POSITION)


async def test_shutter_supported_features_includes_set_position(
    hass: HomeAssistant,
) -> None:
    """SHUTTER exposes OPEN/CLOSE/STOP/SET_POSITION."""
    from homeassistant.components.cover import CoverEntityFeature

    dev = _cover_device(12, "SHUTTER", state=0)
    coord = _StubCoordinator(devices={12: dev})
    ent = SwitchBeeCover(coord, dev)
    features = ent.supported_features
    assert features & CoverEntityFeature.OPEN
    assert features & CoverEntityFeature.CLOSE
    assert features & CoverEntityFeature.STOP
    assert features & CoverEntityFeature.SET_POSITION
