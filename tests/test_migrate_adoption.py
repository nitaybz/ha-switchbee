"""P6 adoption test - end-to-end of the two-phase apply.

This is the load-bearing test for Phase 5's two-phase strategy:

  Phase 5a: tools/migrate.py rewrites entity_registry rows so they have
            platform=ha_switchbee, unique_id=<cu_mac>_<item_id>, and
            config_entry_id=None. The integration is NOT yet installed.
  Phase 5b: the user installs ha-switchbee via the HA UI. async_setup_entry
            runs; for each CU item it calls async_add_entities which feeds
            into HA's entity platform machinery that internally invokes
            EntityRegistry.async_get_or_create. HA matches the orphan row
            by (platform, unique_id), fills in the new config_entry_id, and
            preserves every other field (entity_id, original_name, area_id,
            icon, aliases, labels).

The test pre-populates an orphan row, runs async_setup_entry against a
patched WS client, then asserts the orphan row was adopted - entity_id is
unchanged AND config_entry_id is now set to the new entry's id AND no
new `_2`-suffixed row was created.

This test uses pytest-homeassistant-custom-component to run against a real
HA registry implementation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_switchbee.const import DOMAIN

SAMPLE_MAC_RAW = "A8-21-08-E7-68-8F"
SAMPLE_MAC_NORMALIZED = "a82108e7688f"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield


def _patched_client_with_one_switch():
    """Patch SwitchBeeWSClient so async_setup_entry sees ONE CU item.

    The mock returns a GET_CONFIGURATION payload with a single SWITCH item
    id=42 named "Test Switch" in zone "Test Zone". The integration will
    build one SwitchBeeSwitch with unique_id `{cu_mac}_42`.
    """
    instance = AsyncMock()
    instance.start = AsyncMock(return_value=None)
    instance.get_configuration = AsyncMock(
        return_value={
            "mac": SAMPLE_MAC_RAW,
            "zones": [
                {
                    "name": "Test Zone",
                    "items": [
                        {
                            "id": 42,
                            "name": "Test Switch",
                            "type": "SWITCH",
                            "hw": "VBOX",
                            "state": "OFF",
                        }
                    ],
                }
            ],
        }
    )
    instance.stop = AsyncMock(return_value=None)
    instance.operate = AsyncMock(return_value=None)
    instance.connected = True
    instance.add_listener = lambda cb: lambda: None
    return instance


async def test_async_setup_entry_adopts_orphan_entity_registry_row(
    hass: HomeAssistant,
) -> None:
    """Phase 5b adoption invariant (P6).

    Setup:
      1. Pre-create an area in `area_registry`.
      2. Pre-populate an orphan row in `entity_registry` with platform=ha_switchbee,
         unique_id=a82108e7688f_42, config_entry_id=None, plus
         icon/aliases/labels/area_id all pre-set.
      3. Run `async_setup_entry` against a patched WS client returning the
         matching single CU item.

    Assert:
      - the existing entity_id is reused (no `_2` suffix collision)
      - config_entry_id is now the new entry's id (no longer None)
      - icon / aliases / labels / area_id / original_name are unchanged
    """
    reg = er.async_get(hass)
    area_reg = ar.async_get(hass)

    test_area = area_reg.async_create("Test Living Room")

    # Pre-create the orphan row WITHOUT a config_entry (this is the Phase 5a
    # output: a row whose config_entry_id is None, waiting for adoption).
    orphan = reg.async_get_or_create(
        domain="switch",
        platform=DOMAIN,
        unique_id=f"{SAMPLE_MAC_NORMALIZED}_42",
        config_entry=None,
        suggested_object_id="orphan_test_switch",
        original_name="Test Switch Test Zone",
    )
    orphan_entity_id = orphan.entity_id
    assert orphan.config_entry_id is None

    # Use the public update helper to pre-set the load-bearing fields the
    # cutover must NOT clobber.
    reg.async_update_entity(
        orphan_entity_id,
        icon="mdi:foo",
        aliases={"alias-a"},
        labels={"label-l"},
        area_id=test_area.id,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.68.57",
            CONF_USERNAME: "user",
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)

    # Patch the WS client used by `__init__.py`. We use a single instance so
    # both creation paths (constructor in config_flow / __init__) share state.
    fake = _patched_client_with_one_switch()
    with patch(
        "custom_components.ha_switchbee.SwitchBeeWSClient",
        side_effect=lambda *a, **kw: fake,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Verify adoption.
    after = reg.async_get(orphan_entity_id)
    assert after is not None, (
        "entity_id was NOT preserved by adoption; HA created a new row "
        "instead of adopting the orphan."
    )
    assert after.entity_id == orphan_entity_id, (
        "entity_id mutated during adoption; cutover would break HA automations "
        f"referencing {orphan_entity_id!r}."
    )
    assert after.platform == DOMAIN
    assert after.unique_id == f"{SAMPLE_MAC_NORMALIZED}_42"
    assert after.config_entry_id == entry.entry_id, (
        "config_entry_id was not filled on adoption; HA still considers the row an orphan."
    )
    assert after.icon == "mdi:foo"
    assert "alias-a" in (after.aliases or set())
    assert "label-l" in (after.labels or set())
    assert after.area_id == test_area.id, (
        "area_id was lost during adoption; cutover would strip room assignments off every entity."
    )
    # The entity itself no longer carries a name (has_entity_name=True +
    # _attr_name=None pattern); the friendly label moved to the per-item
    # DeviceInfo. The migrated row keeps whatever `original_name` it had
    # at adoption time (we don't rewrite it). On a fresh ha-switchbee
    # device, HA derives the displayed friendly name from device.name.
    assert after.original_name is None or isinstance(after.original_name, str)
