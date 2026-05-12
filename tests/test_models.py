"""Tests for typed SwitchBee device records and state codecs."""

from __future__ import annotations

import pytest

from custom_components.ha_switchbee.models import (
    SOMFY_COMMANDS,
    SwitchBeeDevice,
    decode_dimmer,
    decode_on_off,
    decode_shutter,
    encode_dimmer,
    encode_on_off,
    encode_shutter,
    encode_somfy,
)


class TestSwitchBeeDeviceRecord:
    """The canonical CU item shape `{id, name, hw, type, zone, state}`."""

    def test_round_trip_from_cu_item_dict(self):
        cu_item = {
            "id": 75,
            "name": "Blind 2 Living Room",
            "hw": "SB-SH-1",
            "type": "SHUTTER",
            "zone": "Living Room",
            "state": 50,
        }
        device = SwitchBeeDevice.from_cu_item(cu_item)
        assert device.id == 75
        assert device.name == "Blind 2 Living Room"
        assert device.hw == "SB-SH-1"
        assert device.type == "SHUTTER"
        assert device.zone == "Living Room"
        assert device.state == 50

    def test_record_is_frozen(self):
        device = SwitchBeeDevice(
            id=1,
            name="Test",
            hw="SB-SW-1",
            type="SWITCH",
            zone="Kitchen",
            state="ON",
        )
        with pytest.raises((AttributeError, Exception)):
            device.name = "Other"  # type: ignore[misc]

    def test_record_uses_slots(self):
        device = SwitchBeeDevice(
            id=1,
            name="Test",
            hw="SB-SW-1",
            type="SWITCH",
            zone="Kitchen",
            state="ON",
        )
        with pytest.raises((AttributeError, Exception)):
            device.extra_field = "nope"  # type: ignore[attr-defined]


class TestOnOffCodec:
    """ON_OFF: 'ON'/'OFF' string <-> bool, with round-trip identity."""

    def test_decode_on(self):
        assert decode_on_off("ON") is True

    def test_decode_off(self):
        assert decode_on_off("OFF") is False

    def test_encode_true(self):
        assert encode_on_off(True) == "ON"

    def test_encode_false(self):
        assert encode_on_off(False) == "OFF"

    @pytest.mark.parametrize("value", [True, False])
    def test_round_trip(self, value):
        assert decode_on_off(encode_on_off(value)) is value


class TestDimmerCodec:
    """DIMMER: 0-100 int, clamped on encode."""

    def test_decode_passes_int(self):
        assert decode_dimmer(50) == 50

    def test_decode_zero(self):
        assert decode_dimmer(0) == 0

    def test_decode_full(self):
        assert decode_dimmer(100) == 100

    def test_encode_clamps_below_zero(self):
        assert encode_dimmer(-5) == 0

    def test_encode_clamps_above_hundred(self):
        assert encode_dimmer(150) == 100

    def test_encode_within_range_passes(self):
        assert encode_dimmer(75) == 75

    @pytest.mark.parametrize("value", [0, 25, 50, 100])
    def test_round_trip_in_range(self, value):
        assert decode_dimmer(encode_dimmer(value)) == value


class TestShutterCodec:
    """SHUTTER: 0-100 int position, clamped on encode. Optional tilt accepted."""

    def test_decode_position(self):
        assert decode_shutter(0) == 0
        assert decode_shutter(50) == 50
        assert decode_shutter(100) == 100

    def test_encode_clamps_below_zero(self):
        assert encode_shutter(-1) == 0

    def test_encode_clamps_above_hundred(self):
        assert encode_shutter(101) == 100

    def test_encode_passes_in_range(self):
        assert encode_shutter(42) == 42

    @pytest.mark.parametrize("value", [0, 25, 50, 100])
    def test_round_trip(self, value):
        assert decode_shutter(encode_shutter(value)) == value


class TestSomfyCommands:
    """SOMFY: commands 'UP' / 'DOWN' / 'STOP'."""

    def test_command_set_is_exact(self):
        assert frozenset({"UP", "DOWN", "STOP"}) == SOMFY_COMMANDS

    @pytest.mark.parametrize("cmd", ["UP", "DOWN", "STOP"])
    def test_encode_valid_command(self, cmd):
        assert encode_somfy(cmd) == cmd

    def test_encode_rejects_unknown_command(self):
        with pytest.raises(ValueError):
            encode_somfy("FOO")
