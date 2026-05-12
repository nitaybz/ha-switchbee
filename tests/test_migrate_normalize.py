"""Tests for `normalize_name` in the migration mapper.

Per plan Decision #9 and Phase 5 Mapping Algorithm step 6 the name index key is
built by collapsing internal whitespace runs to a single space, stripping
leading/trailing whitespace, then casefolding. The function MUST be idempotent
(applying it twice produces the same result) so an already-normalized index
key compares equal to its own re-normalization.
"""

from __future__ import annotations

import pytest

from tools._migrate.mapper import normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Blind 2 Living Room", "blind 2 living room"),
        ("  Blind 2 Living Room  ", "blind 2 living room"),
        ("Blind 2  Living   Room", "blind 2 living room"),
        ("Blind 2 Living Room ", "blind 2 living room"),
        ("\tBlind 2\nLiving Room\r", "blind 2 living room"),
        ("BLIND 2 LIVING ROOM", "blind 2 living room"),
        ("blind 2 living room", "blind 2 living room"),
    ],
)
def test_normalize_name_canonicalises_whitespace_and_case(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_name_is_idempotent() -> None:
    once = normalize_name("  Blind 2  Living  Room ")
    twice = normalize_name(once)
    assert once == twice == "blind 2 living room"


def test_normalize_name_handles_empty_and_whitespace_only() -> None:
    assert normalize_name("") == ""
    assert normalize_name("   ") == ""
    assert normalize_name("\t\n  \r") == ""
