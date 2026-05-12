"""Discovery-Gate tests for `normalize_cu_mac` (Phase 1 Task 1.0).

Decision #4 / #15: the SwitchBee Central Unit MAC is the stable
identifier for every entity unique_id. It MUST come from
`GET_CONFIGURATION.data.mac` and MUST be normalized to 12 lowercase
hex characters. There is NO host-based fallback. If the CU does not
return a usable mac, the integration must fail loudly with a typed
`CUMACMissingError` so config-flow can surface `cannot_connect` to
the user (Decision #15).
"""

from __future__ import annotations

import pytest

from custom_components.ha_switchbee.switchbee_ws import (
    CUMACMissingError,
    normalize_cu_mac,
)


def test_normalize_cu_mac_hyphenated_uppercase() -> None:
    """Verified live 2026-05-12: the CU returns `mac` as `A8-21-08-E7-68-8F`."""
    assert normalize_cu_mac("A8-21-08-E7-68-8F") == "a82108e7688f"


def test_normalize_cu_mac_colon_form_also_accepted() -> None:
    """Tolerant of alternate separators in case firmware drifts."""
    assert normalize_cu_mac("a8:21:08:e7:68:8f") == "a82108e7688f"


def test_normalize_cu_mac_no_separators() -> None:
    """Already-contiguous 12-hex input is accepted and lowercased."""
    assert normalize_cu_mac("A82108E7688F") == "a82108e7688f"


def test_normalize_cu_mac_missing_raises() -> None:
    """Empty/None input must raise the typed error, never silently fall back."""
    with pytest.raises(CUMACMissingError):
        normalize_cu_mac(None)
    with pytest.raises(CUMACMissingError):
        normalize_cu_mac("")


def test_normalize_cu_mac_malformed_raises() -> None:
    """Non-MAC strings (even if 12 chars long) must raise."""
    with pytest.raises(CUMACMissingError):
        normalize_cu_mac("not-a-mac")
    with pytest.raises(CUMACMissingError):
        # 12 chars but contains non-hex characters
        normalize_cu_mac("ZZZZZZZZZZZZ")
