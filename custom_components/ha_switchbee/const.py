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
