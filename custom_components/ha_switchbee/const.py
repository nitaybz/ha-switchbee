"""Constants for the SwitchBee Local integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final[str] = "ha_switchbee"

# Default SwitchBee Central Unit WebSocket port.
DEFAULT_PORT: Final[int] = 7891

# Config-entry field names.
CONF_HOST: Final[str] = "host"
CONF_USERNAME: Final[str] = "username"
CONF_PASSWORD: Final[str] = "password"

# Coordinator / dispatcher signal names. Placeholder values; real wiring lands
# in Phase 3 once the coordinator exists.
SIGNAL_PUSH: Final[str] = f"{DOMAIN}_push"
SIGNAL_REFRESH: Final[str] = f"{DOMAIN}_refresh"

# Fallback poll interval used only when the WebSocket push stream is down.
# Real value tuned in Phase 3.
DEFAULT_SCAN_INTERVAL_SECONDS: Final[int] = 60

# ---------------------------------------------------------------------------
# Protocol contract (Decision #4 - Discovery Gate).
#
# The CU MAC is the stable unique_id prefix for every entity. It comes from
# `GET_CONFIGURATION.data.mac` and MUST be normalized to 12 lowercase hex
# characters. The field name `"mac"` and the response key `"data"` are
# recorded here so a future firmware change is auditable in one place.
# ---------------------------------------------------------------------------
CU_RESPONSE_DATA_KEY: Final[str] = "data"
CU_MAC_FIELD: Final[str] = "mac"

# Protocol timing defaults. Tuned for a real SwitchBee CU on LAN.
LOGIN_TIMEOUT_SECONDS: Final[float] = 5.0
PER_COMMAND_TIMEOUT_SECONDS: Final[float] = 5.0
RECONNECT_BACKOFF_INITIAL_SECONDS: Final[float] = 1.0
RECONNECT_BACKOFF_CAP_SECONDS: Final[float] = 30.0
