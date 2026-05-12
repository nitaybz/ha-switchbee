"""Phase 0 smoke test.

Asserts that the custom_component package is importable and exposes the
canonical `DOMAIN` constant. This is the RED -> GREEN test for Phase 0.

Importing `const` directly (instead of the package `__init__`) keeps this
test independent of Home Assistant being installed. The full
`async_setup_entry` path is exercised under HA-shaped fixtures starting in
Phase 3.
"""

from __future__ import annotations


def test_domain_constant_is_ha_switchbee() -> None:
    """The integration's domain must be exactly `ha_switchbee` (decision #2)."""
    from custom_components.ha_switchbee.const import DOMAIN

    assert DOMAIN == "ha_switchbee"


def test_manifest_domain_matches_const() -> None:
    """manifest.json and const.py must agree on the domain string."""
    import json
    from pathlib import Path

    from custom_components.ha_switchbee.const import DOMAIN

    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "ha_switchbee"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["domain"] == DOMAIN
