"""Tests for the SwitchBee type -> HA platform mapping table."""

from __future__ import annotations

import pytest

from custom_components.ha_switchbee.mapping import (
    DEFERRED_TYPES,
    MAPPING_TABLE,
    map_type_to_platform,
)

# Every SwitchBee type string the JS source `syncHomeKitCache.js` knows about,
# plus TWO_WAY (the live-probe gap from Decision #14). Strings are verbatim
# from `/Users/nitaybz/Projects/homebridge-switchbee/SwitchBee/syncHomeKitCache.js`.
ALL_SOURCE_TYPES = [
    "SWITCH",
    "TIMED_SWITCH",
    "GROUP_SWITCH",
    "LOCK_GROUP",
    "TIMED_POWER",
    "DIMMER",
    "SCENARIO",
    "ROLLING_SCENARIO",
    "LOUVERED_SHUTTER",
    "SHUTTER",
    "SOMFY",
    "THERMOSTAT",
    "VRF_AC",
    "IR_DEVICE",
    "SENSOR",
    "TWO_WAY",
]


class TestMappingTableCoverage:
    """Every type from the source JS is present in MAPPING_TABLE."""

    @pytest.mark.parametrize("sb_type", ALL_SOURCE_TYPES)
    def test_type_is_in_table(self, sb_type):
        assert sb_type in MAPPING_TABLE


class TestV1Mappings:
    """Phase 4 Task 3.4 PLATFORMS = ['switch', 'light', 'cover', 'scene']."""

    @pytest.mark.parametrize(
        "sb_type",
        ["SWITCH", "TIMED_SWITCH", "GROUP_SWITCH", "TIMED_POWER", "LOCK_GROUP"],
    )
    def test_switch_family_maps_to_switch(self, sb_type):
        assert map_type_to_platform(sb_type) == "switch"

    def test_dimmer_maps_to_light(self):
        assert map_type_to_platform("DIMMER") == "light"

    @pytest.mark.parametrize("sb_type", ["SHUTTER", "LOUVERED_SHUTTER", "SOMFY"])
    def test_shutter_family_maps_to_cover(self, sb_type):
        assert map_type_to_platform(sb_type) == "cover"

    @pytest.mark.parametrize("sb_type", ["SCENARIO", "ROLLING_SCENARIO"])
    def test_scenario_family_maps_to_scene(self, sb_type):
        assert map_type_to_platform(sb_type) == "scene"


class TestDeferredV1_1Types:
    """v1.1 deferred types return None and appear in DEFERRED_TYPES."""

    @pytest.mark.parametrize("sb_type", ["THERMOSTAT", "VRF_AC", "IR_DEVICE", "SENSOR", "TWO_WAY"])
    def test_deferred_returns_none(self, sb_type):
        assert map_type_to_platform(sb_type) is None

    def test_two_way_is_deferred(self):
        assert "TWO_WAY" in DEFERRED_TYPES

    def test_sensor_is_deferred(self):
        assert "SENSOR" in DEFERRED_TYPES

    def test_deferred_set_contains_all_expected(self):
        # Per plan Phase 2 mapping table: SENSOR + TWO_WAY are the v1.1
        # deferred-but-planned types. THERMOSTAT/VRF_AC/IR_DEVICE map to None
        # in v1 but are not the same "deferred to v1.1" set per Decision #14
        # and Decision #4 (which name SENSOR and TWO_WAY specifically).
        assert {"SENSOR", "TWO_WAY"}.issubset(DEFERRED_TYPES)


class TestUnknownTypes:
    """Unknown / future / typo'd types return None, never raise."""

    def test_unknown_type_returns_none(self):
        assert map_type_to_platform("DEFINITELY_NOT_A_TYPE") is None

    def test_empty_string_returns_none(self):
        assert map_type_to_platform("") is None

    def test_unknown_type_does_not_raise(self):
        # Must NOT raise.
        map_type_to_platform("MADE_UP")
        map_type_to_platform("switch")  # case-sensitive: lowercase is unknown

    def test_mapping_is_case_sensitive(self):
        # JS source uses uppercase verbatim; lowercase should miss.
        assert map_type_to_platform("switch") is None
        assert map_type_to_platform("dimmer") is None


class TestMappingTableShape:
    """Table is a dict from SwitchBee type string to HA platform string or None."""

    def test_all_values_are_platform_or_none(self):
        valid_platforms = {"switch", "light", "cover", "scene"}
        for sb_type, platform in MAPPING_TABLE.items():
            assert isinstance(sb_type, str)
            assert platform is None or platform in valid_platforms, (
                f"{sb_type} maps to unexpected platform {platform!r}"
            )
