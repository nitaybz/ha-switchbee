"""Pure-Python unit tests for the config flow's voluptuous schema.

The schema is exported as a constant from `config_flow.py` so it can be
tested without instantiating a HA `ConfigFlow` (which requires the full
HA test harness). These tests are intentionally HA-free.
"""

from __future__ import annotations

import pytest
import voluptuous as vol

from custom_components.ha_switchbee.config_flow import (
    OPTIONS_SCHEMA,
    USER_DATA_SCHEMA,
)


class TestUserDataSchema:
    """Schema for the initial `user` step (host + username + password)."""

    def test_accepts_complete_input(self) -> None:
        result = USER_DATA_SCHEMA(
            {
                "host": "192.168.68.57",
                "username": "user",
                "password": "secret",
            }
        )
        assert result["host"] == "192.168.68.57"
        assert result["username"] == "user"
        assert result["password"] == "secret"

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(vol.Invalid):
            USER_DATA_SCHEMA({"username": "user", "password": "secret"})

    def test_rejects_missing_username(self) -> None:
        with pytest.raises(vol.Invalid):
            USER_DATA_SCHEMA({"host": "192.168.68.57", "password": "secret"})

    def test_rejects_missing_password(self) -> None:
        with pytest.raises(vol.Invalid):
            USER_DATA_SCHEMA({"host": "192.168.68.57", "username": "user"})

    def test_rejects_empty_host(self) -> None:
        with pytest.raises(vol.Invalid):
            USER_DATA_SCHEMA({"host": "", "username": "user", "password": "secret"})

    def test_rejects_empty_username(self) -> None:
        with pytest.raises(vol.Invalid):
            USER_DATA_SCHEMA({"host": "192.168.68.57", "username": "", "password": "secret"})

    def test_rejects_empty_password(self) -> None:
        with pytest.raises(vol.Invalid):
            USER_DATA_SCHEMA({"host": "192.168.68.57", "username": "user", "password": ""})


class TestOptionsSchema:
    """Schema for the OptionsFlow (connection timeout tunable)."""

    def test_accepts_default_timeout(self) -> None:
        # The schema has a default; passing an empty dict should fill it in.
        result = OPTIONS_SCHEMA({})
        assert result["connection_timeout"] == 5

    def test_accepts_custom_timeout(self) -> None:
        result = OPTIONS_SCHEMA({"connection_timeout": 10})
        assert result["connection_timeout"] == 10

    def test_rejects_zero_timeout(self) -> None:
        with pytest.raises(vol.Invalid):
            OPTIONS_SCHEMA({"connection_timeout": 0})

    def test_rejects_negative_timeout(self) -> None:
        with pytest.raises(vol.Invalid):
            OPTIONS_SCHEMA({"connection_timeout": -1})

    def test_rejects_non_int_timeout(self) -> None:
        with pytest.raises(vol.Invalid):
            OPTIONS_SCHEMA({"connection_timeout": "five"})
