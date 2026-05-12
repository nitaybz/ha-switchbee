"""Unit tests for the SwitchBeeEntity base class.

The base entity inherits from HA's `CoordinatorEntity` and provides:
- a unique_id in the format `{cu_mac}_{item_id}` where `cu_mac` is the
  lowercase 12-hex normalized MAC locked in Phase 3.
- a `DeviceInfo` with `identifiers = {(DOMAIN, cu_mac)}` so every entity
  rides on the SwitchBee CU device card.
- an `available` property that is False when the coordinator's WS is
  disconnected OR when the cached state is the OFFLINE sentinel
  (`-1` or `"OFFLINE"`).
- subscription to the coordinator's per-item dispatcher signal in
  `async_added_to_hass` / `async_will_remove_from_hass`.
"""

from __future__ import annotations

import re

import pytest

from custom_components.ha_switchbee.const import DOMAIN
from custom_components.ha_switchbee.entity import SwitchBeeEntity
from custom_components.ha_switchbee.models import SwitchBeeDevice


class _StubClient:
    """Minimal stand-in for SwitchBeeWSClient (no real socket)."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.operate_calls: list[tuple[int, object]] = []

    async def operate(self, item_id: int, value: object) -> dict:
        self.operate_calls.append((item_id, value))
        return {"status": "OK"}

    async def stop(self) -> None:
        self.connected = False


class _StubCoordinator:
    """Bare-bones coordinator that exposes the surface entities need."""

    def __init__(
        self,
        *,
        cu_mac: str = "a82108e7688f",
        devices: dict[int, SwitchBeeDevice] | None = None,
        client: _StubClient | None = None,
    ) -> None:
        self.cu_mac = cu_mac
        self.client = client or _StubClient()
        self.devices = devices or {}
        self.data: dict[int, object] = {
            item_id: dev.state for item_id, dev in self.devices.items()
        }
        # Track last_update_success the way DataUpdateCoordinator does so
        # the CoordinatorEntity.available super().available check has a
        # truthy value.
        self.last_update_success = True

    def signal_for(self, item_id: int) -> str:
        return f"{DOMAIN}_push_{self.cu_mac}_{item_id}"

    # CoordinatorEntity inspects these helpers on attach/detach. The base
    # class's async_added_to_hass uses async_add_listener; provide it as a
    # no-op so we do not need a full DataUpdateCoordinator.
    def async_add_listener(self, update_callback, context=None):
        def _remove() -> None:
            return None

        return _remove


def _make_device(item_id: int, type_: str = "SWITCH", state: object = "OFF") -> SwitchBeeDevice:
    return SwitchBeeDevice(
        id=item_id,
        name=f"Item {item_id}",
        hw="hw",
        type=type_,
        zone="Zone",
        state=state,
    )


def test_unique_id_matches_cu_mac_underscore_item_id() -> None:
    """unique_id is `{cu_mac}_{item_id}` with lowercase 12-hex cu_mac."""
    dev = _make_device(7, type_="DIMMER", state=50)
    coordinator = _StubCoordinator(devices={7: dev})
    entity = SwitchBeeEntity(coordinator, dev)
    assert entity.unique_id == "a82108e7688f_7"
    assert re.match(r"^[0-9a-f]{12}_\d+$", entity.unique_id)


def test_device_info_has_per_item_identifier_and_via_device() -> None:
    """Each entity has its own per-item device anchored under the CU bridge.

    `identifiers` carries the per-item key `(DOMAIN, "{cu_mac}_{item_id}")`
    and `via_device` points at the CU bridge `(DOMAIN, cu_mac)`. The CU
    bridge device itself is registered separately in `async_setup_entry`.
    The device-level `name` is the full friendly label so HA can display
    each switch as its own card without the "SwitchBee Central Unit"
    prefix; `_attr_name = None` on the entity keeps HA from concatenating
    the device name with an entity name.
    """
    dev = _make_device(3)
    coordinator = _StubCoordinator(devices={3: dev})
    entity = SwitchBeeEntity(coordinator, dev)
    info = entity.device_info
    assert info is not None
    assert info["identifiers"] == {(DOMAIN, "a82108e7688f_3")}
    assert info["via_device"] == (DOMAIN, "a82108e7688f")
    assert info["manufacturer"] == "SwitchBee"
    assert info["model"] == "SWITCH"
    assert info["name"] == "Item 3 Zone"
    assert info.get("suggested_area") == "Zone"


def test_available_false_when_ws_disconnected() -> None:
    """available is False if the coordinator's client is not connected."""
    dev = _make_device(3, state="OFF")
    coordinator = _StubCoordinator(
        devices={3: dev}, client=_StubClient(connected=False)
    )
    entity = SwitchBeeEntity(coordinator, dev)
    assert entity.available is False


def test_available_false_when_state_is_minus_one_sentinel() -> None:
    """available is False if cached state is the integer OFFLINE sentinel."""
    dev = _make_device(3, state=-1)
    coordinator = _StubCoordinator(devices={3: dev})
    coordinator.data[3] = -1
    entity = SwitchBeeEntity(coordinator, dev)
    assert entity.available is False


def test_available_false_when_state_is_offline_string() -> None:
    """available is False if cached state is the string OFFLINE sentinel."""
    dev = _make_device(3, state="OFFLINE")
    coordinator = _StubCoordinator(devices={3: dev})
    coordinator.data[3] = "OFFLINE"
    entity = SwitchBeeEntity(coordinator, dev)
    assert entity.available is False


def test_available_true_when_connected_and_state_ok() -> None:
    """available is True when WS is up and state is a normal value."""
    dev = _make_device(3, state="ON")
    coordinator = _StubCoordinator(devices={3: dev})
    coordinator.data[3] = "ON"
    entity = SwitchBeeEntity(coordinator, dev)
    assert entity.available is True


def test_entity_name_is_none_so_device_name_is_used_verbatim() -> None:
    """has_entity_name=True + _attr_name=None makes HA use device.name as-is.

    Apple Home (via HA's homekit integration) would otherwise emit
    `<device.name> <_attr_name>` and prefix each accessory with the
    device name. Setting `_attr_name = None` is the canonical HA pattern
    for the "this entity IS its device" case.

    The full friendly label lives on the device (`device_info.name`).
    """
    dev = _make_device(3)
    coordinator = _StubCoordinator(devices={3: dev})
    entity = SwitchBeeEntity(coordinator, dev)
    assert entity.has_entity_name is True
    assert entity.name is None
    info = entity.device_info
    assert info is not None
    assert info["name"] == "Item 3 Zone"


def test_device_name_falls_back_to_item_name_when_zone_empty() -> None:
    """Items with no zone assignment use just the device name on the device."""
    dev = SwitchBeeDevice(id=4, name="Lone Item", hw="hw", type="SWITCH", zone="")
    coordinator = _StubCoordinator(devices={4: dev})
    entity = SwitchBeeEntity(coordinator, dev)
    info = entity.device_info
    assert info is not None
    assert info["name"] == "Lone Item"
    assert entity.name is None


@pytest.mark.parametrize(
    "raw_mac",
    [
        "a82108e7688f",
        "A82108E7688F",  # uppercase variants are normalized before getting here
    ],
)
def test_unique_id_uses_cu_mac_as_given(raw_mac: str) -> None:
    """Whatever lowercase-or-not mac the coordinator provides is used verbatim.

    Normalization is the coordinator's job; the entity just consumes it.
    """
    dev = _make_device(99)
    coordinator = _StubCoordinator(cu_mac=raw_mac.lower(), devices={99: dev})
    entity = SwitchBeeEntity(coordinator, dev)
    assert entity.unique_id == f"{raw_mac.lower()}_99"
