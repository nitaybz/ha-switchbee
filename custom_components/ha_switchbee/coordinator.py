"""DataUpdateCoordinator for the SwitchBee Local integration.

This module owns the WebSocket client lifecycle once the integration is set
up by `async_setup_entry`, fans out CONFIGURATION_CHANGE push events to
platform entities via HA's dispatcher, and runs the LOGIN-timeout watchdog
that is the Python expression of the cron-driven `kill homebridge if LOGIN
never returns` workaround we put on Moshe's machine.

The watchdog state machine is intentionally a pure-Python helper
(`LoginTimeoutWatchdog`) so it can be unit-tested without Home Assistant
installed (see `tests/test_coordinator_internal.py`).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from .const import DOMAIN, SIGNAL_PUSH
from .mapping import map_type_to_platform
from .models import SwitchBeeDevice
from .switchbee_ws import PushEvent, SwitchBeeWSClient, normalize_cu_mac

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Watchdog defaults (Plan Decision: cron-watchdog port).
# Three LOGIN timeouts inside a five-minute rolling window means the CU is
# wedged in the way that nightly bit Moshe's homebridge listener; drop the
# cached token and force a full reconnect.
LOGIN_TIMEOUT_THRESHOLD: int = 3
LOGIN_TIMEOUT_WINDOW_SECONDS: float = 300.0


class LoginTimeoutWatchdog:
    """Rolling-window counter for LOGIN timeouts.

    A timeout is `recorded` with a wall-clock-ish timestamp (the tests use
    a synthetic clock; the live integration will use `time.monotonic()`).
    The watchdog considers itself `tripped` when at least `threshold`
    timeouts fall inside the most recent `window_seconds` window.

    The state machine is deliberately tiny and synchronous so it can be
    exercised under a plain Python 3.12 venv with no HA installed.
    """

    __slots__ = ("_threshold", "_window", "_events")

    def __init__(self, *, threshold: int, window_seconds: float) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold!r}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds!r}")
        self._threshold = threshold
        self._window = window_seconds
        self._events: deque[float] = deque()

    def record_timeout(self, *, now: float) -> None:
        """Record a LOGIN timeout that occurred at wall-clock `now`."""
        self._events.append(now)
        self._evict_old(now=now)

    def reset(self) -> None:
        """Drop every recorded timeout."""
        self._events.clear()

    def count(self) -> int:
        """Return the number of timeouts currently inside the window.

        Note: count() does NOT re-evict; eviction happens on
        `record_timeout`. Callers reading count() between record_timeout()
        calls will see the post-eviction value from the last record.
        """
        return len(self._events)

    def tripped(self) -> bool:
        """True if `threshold` or more timeouts are inside the window."""
        return len(self._events) >= self._threshold

    def _evict_old(self, *, now: float) -> None:
        """Drop events older than `window_seconds` from `now`.

        Events at exactly the boundary (`now - window`) are evicted: this
        keeps the window half-open `(now - window, now]` which is the
        natural reading of "in the last five minutes".
        """
        cutoff = now - self._window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()


class SwitchBeeCoordinator:
    """Coordinator gluing the WS client to HA's dispatcher.

    The coordinator does NOT subclass HA's `DataUpdateCoordinator` directly
    in this module: the import would force every consumer of this file
    (including the pure-Python watchdog tests) to install Home Assistant.
    The HA-aware integration glue in `__init__.py` is free to wrap this
    coordinator into an `HassCoordinator` if a polling interval is ever
    needed; v1's source of truth is the push stream, so a coordinator that
    only fans out push events is sufficient.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: SwitchBeeWSClient,
        *,
        cu_mac: str,
        devices: dict[int, SwitchBeeDevice],
    ) -> None:
        self.hass = hass
        self.client = client
        self.cu_mac = cu_mac
        self.devices = devices
        self.data: dict[int, Any] = {item_id: device.state for item_id, device in devices.items()}
        self.watchdog = LoginTimeoutWatchdog(
            threshold=LOGIN_TIMEOUT_THRESHOLD,
            window_seconds=LOGIN_TIMEOUT_WINDOW_SECONDS,
        )
        self._unsub_listener = client.add_listener(self._on_push)
        # HA `CoordinatorEntity.async_added_to_hass` calls
        # `coordinator.async_add_listener(update_callback)` at attach time.
        # We are not subclassing DataUpdateCoordinator (see class docstring),
        # so satisfy that contract ourselves with a tiny callback registry.
        # Each entry is a zero-arg callable; called whenever push state changes
        # so CoordinatorEntity can recompute its derived state. The per-item
        # dispatcher signal (added by entity.async_added_to_hass) is the
        # finer-grained path actually used in v1; this fan-out exists to keep
        # the CoordinatorEntity attach contract honored.
        self._update_listeners: list[Callable[[], None]] = []
        # Stays in sync with `data` for compatibility with downstream code
        # that reads `last_update_success` from a DataUpdateCoordinator.
        self.last_update_success = True

    def devices_by_platform(self) -> dict[str, list[SwitchBeeDevice]]:
        """Group the device dict by HA platform string.

        Items whose `type` maps to `None` (deferred / out-of-v1-scope) are
        omitted. Used by `async_setup_entry` to filter the device set per
        platform forward-entry-setup call.
        """
        result: dict[str, list[SwitchBeeDevice]] = {}
        for device in self.devices.values():
            platform = map_type_to_platform(device.type)
            if platform is None:
                continue
            result.setdefault(platform, []).append(device)
        return result

    def _on_push(self, event: PushEvent) -> None:
        """Handle a CONFIGURATION_CHANGE event from the WS client.

        Updates the cached state for the item id and dispatches a signal
        so platform entities subscribed by `unique_id` can re-render.
        """
        self.data[event.id] = event.value
        # Lazy import so the watchdog tests do not need HA.
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        async_dispatcher_send(
            self.hass,
            self._signal_for(event.id),
            event.value,
        )
        # Fan out to coordinator-level listeners (CoordinatorEntity contract).
        # Iterate a copy so a listener may unsubscribe itself.
        for cb in list(self._update_listeners):
            try:
                cb()
            except Exception:  # pragma: no cover
                _LOGGER.exception("coordinator listener raised")

    def async_add_listener(
        self,
        update_callback: Callable[[], None],
        context: Any = None,
    ) -> Callable[[], None]:
        """Register a zero-arg callback fired on every push update.

        Matches the `DataUpdateCoordinator.async_add_listener` signature
        that HA's `CoordinatorEntity.async_added_to_hass` calls at attach
        time. Returns an unsubscribe callable.

        The `context` arg is accepted for HA-API parity and ignored: the
        per-item dispatcher signal (`signal_for(item_id)`) is the path
        actually used by platform entities; this fan-out is only the
        coarse-grained CoordinatorEntity hook.
        """
        del context  # unused, parity with HA's signature
        self._update_listeners.append(update_callback)

        def _remove() -> None:
            import contextlib

            with contextlib.suppress(ValueError):
                self._update_listeners.remove(update_callback)

        return _remove

    @property
    def update_interval(self) -> None:
        """CoordinatorEntity checks this; v1 is push-only, no polling."""
        return None

    async def async_request_refresh(self) -> None:
        """No-op. CoordinatorEntity calls this on attach in some HA versions.

        v1 is push-only: state arrives via CONFIGURATION_CHANGE notifications,
        not polled refreshes. A request-refresh fires a one-shot pull of the
        CU state via `GET_MULTIPLE_STATES` if a future caller needs it; for
        now we are a no-op because the WS subscription is already live.
        """
        return None

    def _signal_for(self, item_id: int) -> str:
        """Dispatcher signal name for one item id."""
        return f"{SIGNAL_PUSH}_{self.cu_mac}_{item_id}"

    def signal_for(self, item_id: int) -> str:
        """Public signal name accessor for platform entities."""
        return self._signal_for(item_id)

    async def async_shutdown(self) -> None:
        """Unsubscribe the push listener and stop the WS client."""
        self._unsub_listener()
        await self.client.stop()


async def async_build_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: SwitchBeeWSClient,
) -> SwitchBeeCoordinator:
    """Open the WS, fetch GET_CONFIGURATION, and build a coordinator.

    Used by `async_setup_entry`. Failure modes:
      - `CUMACMissingError` -> propagates (caller maps to ConfigEntryNotReady).
      - any other exception -> propagates after the client is stopped.
    """
    await client.start()
    try:
        raw_config = await client.get_configuration()
    except Exception:
        await client.stop()
        raise

    mac_raw = raw_config.get("mac")
    cu_mac = normalize_cu_mac(mac_raw)
    devices = _devices_from_config(raw_config.get("zones", []))
    return SwitchBeeCoordinator(hass, client, cu_mac=cu_mac, devices=devices)


def _devices_from_config(zones: Iterable[Any]) -> dict[int, SwitchBeeDevice]:
    """Flatten the zones array into a {item_id: SwitchBeeDevice} dict."""
    devices: dict[int, SwitchBeeDevice] = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_name = str(zone.get("name", ""))
        for item in zone.get("items", []):
            if not isinstance(item, dict) or "id" not in item:
                continue
            # Inject the zone name so SwitchBeeDevice.from_cu_item picks it up
            # without mutating the original dict.
            payload = {**item, "zone": item.get("zone", zone_name)}
            device = SwitchBeeDevice.from_cu_item(payload)
            devices[device.id] = device
    return devices


__all__ = [
    "LOGIN_TIMEOUT_THRESHOLD",
    "LOGIN_TIMEOUT_WINDOW_SECONDS",
    "LoginTimeoutWatchdog",
    "SwitchBeeCoordinator",
    "async_build_coordinator",
]

# Bind for type checkers that strip TYPE_CHECKING imports at runtime.
_ = DOMAIN
