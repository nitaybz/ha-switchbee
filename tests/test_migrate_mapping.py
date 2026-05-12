"""Tests for `tools._migrate.mapper.map_entities`.

Covers the five confidence outcomes called out by Phase 5 Mapping Algorithm:

- high     - exact normalized-name match against a single CU device
- medium   - duplicate normalized name disambiguated by the area->zone tie-breaker
- low      - duplicate name AND the tie-breaker still fails OR no candidate at all
- delete   - `button.*_identify` entries
- keep_homekit - SwitchBee SENSOR / TWO_WAY items (and the v1.1 climate/remote types)

Both `_aid_iid` (2-part) and `_aid_sid_iid` (3-part) homekit_controller
unique_id shapes are exercised.
"""

from __future__ import annotations

from tools._migrate.mapper import MappingRow, map_entities

BRIDGE_MAC = "0E:0F:B5:1B:3D:37"
CU_MAC = "a82108e7688f"


def _entity(
    *,
    entity_id: str,
    unique_id: str,
    original_name: str,
    platform: str = "homekit_controller",
    area_id: str | None = None,
) -> dict[str, object]:
    return {
        "entity_id": entity_id,
        "unique_id": unique_id,
        "original_name": original_name,
        "platform": platform,
        "area_id": area_id,
    }


def test_high_confidence_exact_match() -> None:
    """An HA entity whose original_name matches `name + ' ' + zone` exactly."""
    cu_devices = {
        42: {"id": 42, "name": "Blind 2", "type": "SHUTTER", "zone": "Living Room"},
    }
    entities = [
        _entity(
            entity_id="cover.blind_2_living_room",
            unique_id=f"{BRIDGE_MAC}_75_8",
            original_name="Blind 2 Living Room",
        )
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    assert len(rows) == 1
    row = rows[0]
    assert row.confidence == "high"
    assert row.action == "migrate"
    assert row.new_unique_id == f"{CU_MAC}_42"
    assert row.item_id == 42
    assert row.sb_type == "SHUTTER"


def test_high_confidence_three_segment_unique_id() -> None:
    """The mapper accepts the `_aid_sid_iid` 3-int trailing shape too."""
    cu_devices = {
        7: {"id": 7, "name": "Kitchen Light", "type": "DIMMER", "zone": "Kitchen"},
    }
    entities = [
        _entity(
            entity_id="light.kitchen_light_kitchen",
            unique_id=f"{BRIDGE_MAC}_1_1_2",
            original_name="Kitchen Light Kitchen",
        )
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    assert len(rows) == 1
    assert rows[0].confidence == "high"
    assert rows[0].action == "migrate"
    assert rows[0].new_unique_id == f"{CU_MAC}_7"


def test_high_confidence_normalization_handles_trailing_whitespace() -> None:
    """Real Moshe fixture has trailing whitespace on HA original_name."""
    cu_devices = {
        42: {"id": 42, "name": "Blind 2", "type": "SHUTTER", "zone": "Living Room"},
    }
    entities = [
        _entity(
            entity_id="cover.blind_2_living_room",
            unique_id=f"{BRIDGE_MAC}_75_8",
            original_name="Blind 2 Living Room ",
        )
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    assert rows[0].confidence == "high"
    assert rows[0].action == "migrate"


def test_medium_confidence_via_tie_breaker() -> None:
    """Two CU items normalize identically; area->zone tie-breaker disambiguates."""
    # Build the input so both candidates share the SAME normalized full
    # name to force the tie-breaker path.
    cu_devices_collision = {
        10: {"id": 10, "name": "Switch", "type": "SWITCH", "zone": "Kitchen"},
        11: {"id": 11, "name": "Switch", "type": "SWITCH", "zone": "Kitchen"},
    }
    entities = [
        _entity(
            entity_id="switch.switch_kitchen",
            unique_id=f"{BRIDGE_MAC}_5_3",
            original_name="Switch Kitchen",
            area_id="area_kitchen",
        )
    ]
    area_registry = {"area_kitchen": "Kitchen"}
    rows = map_entities(
        entities,
        cu_devices=cu_devices_collision,
        cu_mac=CU_MAC,
        bridge_mac=BRIDGE_MAC,
        area_registry=area_registry,
    )
    # Both candidates have zone="Kitchen" so the tie-breaker still matches
    # BOTH; this is the genuinely-ambiguous case and produces low+keep_homekit.
    assert rows[0].action == "keep_homekit"
    assert rows[0].confidence == "low"

    # Now construct a real disambiguation: same NAME but different zones,
    # arranged so the normalized full-name keys collide.
    cu_devices_disambig = {
        10: {"id": 10, "name": "Mirror Switch", "type": "SWITCH", "zone": "Mirror"},
        11: {"id": 11, "name": "Mirror", "type": "SWITCH", "zone": "Switch Mirror"},
    }
    # device 10 normalizes to "mirror switch mirror"
    # device 11 normalizes to "mirror switch mirror"  (collision!)
    entities2 = [
        _entity(
            entity_id="switch.mirror_switch_mirror",
            unique_id=f"{BRIDGE_MAC}_8_1",
            original_name="Mirror Switch Mirror",
            area_id="area_mirror_room",
        )
    ]
    area_registry2 = {"area_mirror_room": "Mirror"}
    rows2 = map_entities(
        entities2,
        cu_devices=cu_devices_disambig,
        cu_mac=CU_MAC,
        bridge_mac=BRIDGE_MAC,
        area_registry=area_registry2,
    )
    # device 10 has zone "Mirror" which matches area "Mirror"; device 11 zone
    # is "Switch Mirror" which does NOT. Tie-breaker disambiguates -> medium.
    assert rows2[0].confidence == "medium"
    assert rows2[0].action == "migrate"
    assert rows2[0].new_unique_id == f"{CU_MAC}_10"


def test_low_confidence_no_match() -> None:
    """No CU device matches; mark as keep_homekit with low confidence."""
    cu_devices = {
        1: {"id": 1, "name": "Other", "type": "SWITCH", "zone": "Other Zone"},
    }
    entities = [
        _entity(
            entity_id="cover.unknown_blind",
            unique_id=f"{BRIDGE_MAC}_99_2",
            original_name="Unknown Blind Some Zone",
        )
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    assert rows[0].confidence == "low"
    assert rows[0].action == "keep_homekit"
    assert rows[0].new_unique_id is None


def test_button_identify_entries_are_deleted() -> None:
    """Decision #12: button.*_identify entries always action=delete."""
    cu_devices = {
        42: {"id": 42, "name": "Blind 2", "type": "SHUTTER", "zone": "Living Room"},
    }
    entities = [
        _entity(
            entity_id="button.blind_2_living_room_identify",
            unique_id=f"{BRIDGE_MAC}_75_1",
            original_name="Identify",
        )
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    assert rows[0].action == "delete"
    assert rows[0].new_unique_id is None


def test_sensor_and_two_way_items_keep_homekit_even_on_high_match() -> None:
    """Decision #14: SENSOR / TWO_WAY items mark keep_homekit, not migrate."""
    cu_devices = {
        50: {"id": 50, "name": "Motion", "type": "SENSOR", "zone": "Hallway"},
        51: {"id": 51, "name": "Inline Two", "type": "TWO_WAY", "zone": "Garage"},
    }
    entities = [
        _entity(
            entity_id="binary_sensor.motion_hallway",
            unique_id=f"{BRIDGE_MAC}_4_1",
            original_name="Motion Hallway",
        ),
        _entity(
            entity_id="switch.inline_two_garage",
            unique_id=f"{BRIDGE_MAC}_6_1",
            original_name="Inline Two Garage",
        ),
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    actions = {r.entity_id: r.action for r in rows}
    assert actions["binary_sensor.motion_hallway"] == "keep_homekit"
    assert actions["switch.inline_two_garage"] == "keep_homekit"
    # But the rows still report the matched item id and sb_type for the
    # report, so an operator can verify the keep-on-purpose decision.
    motion = next(r for r in rows if r.entity_id == "binary_sensor.motion_hallway")
    assert motion.item_id == 50
    assert motion.sb_type == "SENSOR"


def test_non_switchbee_entities_are_skipped() -> None:
    """An entity whose platform is not homekit_controller is ignored."""
    cu_devices = {1: {"id": 1, "name": "X", "type": "SWITCH", "zone": "Z"}}
    entities = [
        _entity(
            entity_id="sensor.weather",
            unique_id="weather_provider_abc",
            original_name="Outside Temperature",
            platform="weather",
        )
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    assert rows == []


def test_homekit_entity_with_unrelated_bridge_is_skipped() -> None:
    """A homekit_controller entity whose unique_id does NOT start with the
    SwitchBee bridge MAC is left alone (could be a Hue bridge etc)."""
    cu_devices = {1: {"id": 1, "name": "X", "type": "SWITCH", "zone": "Z"}}
    entities = [
        _entity(
            entity_id="light.hue_bulb",
            unique_id="AA:BB:CC:DD:EE:FF_1_1",
            original_name="Hue Bulb Living Room",
        )
    ]
    rows = map_entities(entities, cu_devices=cu_devices, cu_mac=CU_MAC, bridge_mac=BRIDGE_MAC)
    assert rows == []


def test_mapping_row_is_dataclass_with_named_fields() -> None:
    """Sanity check on the public dataclass surface."""
    row = MappingRow(
        entity_id="cover.x",
        old_unique_id="0E:0F:B5:1B:3D:37_1_1",
        new_unique_id=None,
        confidence="low",
        action="keep_homekit",
        reason="probe",
    )
    assert row.entity_id == "cover.x"
    assert row.action == "keep_homekit"


# ----- SerialNumber-based primary mapping path (Phase 5 revised, Decision #9
# revised after live verification on the STE Smart Home CU) -----


def _device(*, device_id: str, serial_number: str | None) -> dict[str, object]:
    """Build a minimal HA device_registry row for SN-based mapping tests."""
    return {
        "id": device_id,
        "serial_number": serial_number,
        "identifiers": [["homekit_controller:accessory-id", "dummy"]],
    }


def test_sn_path_resolves_when_name_does_not_match() -> None:
    """SerialNumber-based mapping succeeds even when CU was renamed since pairing.

    Reproduces the live finding on Moshe's STE Smart Home CU: HA still has
    the old `Cistercian Outside ` original_name, but the CU has since
    renamed the item, so name matching returns no candidate. The SN
    `REGULAR_SWITCH_ID152` recovers item.id 152 directly.
    """
    cu_devices = {
        152: {"id": 152, "name": "Visteria", "type": "SWITCH", "zone": "Outside "},
    }
    ha_devices = [
        _device(
            device_id="0718733d39d3a5809459bd50ad12c41b",
            serial_number="REGULAR_SWITCH_ID152",
        )
    ]
    entities = [
        {
            "entity_id": "switch.cistercian_outside",
            "unique_id": f"{BRIDGE_MAC}_31_8",
            "original_name": "Cistercian Outside ",
            "platform": "homekit_controller",
            "area_id": None,
            "device_id": "0718733d39d3a5809459bd50ad12c41b",
        }
    ]
    rows = map_entities(
        entities,
        cu_devices=cu_devices,
        cu_mac=CU_MAC,
        bridge_mac=BRIDGE_MAC,
        ha_devices=ha_devices,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "migrate"
    assert row.confidence == "high"
    assert row.new_unique_id == f"{CU_MAC}_152"
    assert row.item_id == 152
    assert row.sb_type == "SWITCH"
    assert row.reason == "HomeKit SerialNumber -> item.id"


def test_sn_path_supports_somfy_serial_prefix() -> None:
    """SOMFY accessories use SN prefix `SOMFY_ID<n>`."""
    cu_devices = {
        471: {"id": 471, "name": "Blind 2", "type": "SOMFY", "zone": "Living Room"},
    }
    ha_devices = [_device(device_id="dev-somfy", serial_number="SOMFY_ID471")]
    entities = [
        {
            "entity_id": "cover.blind_2_living_room",
            "unique_id": f"{BRIDGE_MAC}_75_8",
            "original_name": "Blind 2 Living Room ",
            "platform": "homekit_controller",
            "device_id": "dev-somfy",
        }
    ]
    rows = map_entities(
        entities,
        cu_devices=cu_devices,
        cu_mac=CU_MAC,
        bridge_mac=BRIDGE_MAC,
        ha_devices=ha_devices,
    )
    assert rows[0].action == "migrate"
    assert rows[0].new_unique_id == f"{CU_MAC}_471"
    assert rows[0].sb_type == "SOMFY"


def test_sn_path_falls_back_to_name_when_serial_missing() -> None:
    """When ha_devices does not have the entity's device_id, fall back to name."""
    cu_devices = {
        42: {"id": 42, "name": "Test", "type": "SWITCH", "zone": "Living Room"},
    }
    ha_devices: list[dict] = []  # empty -> SN path produces no match
    entities = [
        {
            "entity_id": "switch.test_living_room",
            "unique_id": f"{BRIDGE_MAC}_5_8",
            "original_name": "Test Living Room",
            "platform": "homekit_controller",
            "device_id": "missing",
        }
    ]
    rows = map_entities(
        entities,
        cu_devices=cu_devices,
        cu_mac=CU_MAC,
        bridge_mac=BRIDGE_MAC,
        ha_devices=ha_devices,
    )
    assert rows[0].action == "migrate"
    assert rows[0].confidence == "high"
    assert rows[0].reason == "exact match on (name + zone)"


def test_sn_path_respects_keep_homekit_types() -> None:
    """SN finds the item but the type is TWO_WAY -> keep_homekit."""
    cu_devices = {
        99: {"id": 99, "name": "Two Way", "type": "TWO_WAY", "zone": "Hall"},
    }
    ha_devices = [_device(device_id="dev-twoway", serial_number="TWO_WAY_ID99")]
    entities = [
        {
            "entity_id": "switch.two_way_hall",
            "unique_id": f"{BRIDGE_MAC}_77_8",
            "original_name": "Two Way Hall",
            "platform": "homekit_controller",
            "device_id": "dev-twoway",
        }
    ]
    rows = map_entities(
        entities,
        cu_devices=cu_devices,
        cu_mac=CU_MAC,
        bridge_mac=BRIDGE_MAC,
        ha_devices=ha_devices,
    )
    assert rows[0].action == "keep_homekit"
    assert rows[0].item_id == 99
    assert rows[0].sb_type == "TWO_WAY"


def test_sn_path_ignores_non_id_serials() -> None:
    """The bridge's own SerialNumber is just the MAC; should not match."""
    cu_devices: dict = {}
    ha_devices = [_device(device_id="dev-bridge", serial_number="0E:0F:B5:1B:3D:37")]
    entities = [
        {
            "entity_id": "button.homebridge_switchbee_8db0_identify",
            "unique_id": f"{BRIDGE_MAC}_1_1_2",
            "original_name": "homebridge-switchbee 8DB0 Identify",
            "platform": "homekit_controller",
            "device_id": "dev-bridge",
        }
    ]
    rows = map_entities(
        entities,
        cu_devices=cu_devices,
        cu_mac=CU_MAC,
        bridge_mac=BRIDGE_MAC,
        ha_devices=ha_devices,
    )
    # button.*_identify always deletes; SN doesn't matter, the identify
    # short-circuit still fires.
    assert rows[0].action == "delete"
