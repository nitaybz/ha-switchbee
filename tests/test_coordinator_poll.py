"""Tests for the periodic reconciliation poll in SwitchBeeCoordinator.

The push stream is the integration's primary state path; the poll is a
defensive guard against missed CONFIGURATION_CHANGE events. These tests
exercise `_reconcile_once` (the timing-free core) directly so we do not
have to wait real seconds.

Push-side mocking pattern:
- `_FakeClient.get_multiple_states` returns a programmable dict mapping
  item_id -> state. Tests set `client.next_states` and call
  `coordinator._reconcile_once()` once to inspect the resulting dispatch
  calls.
- Dispatcher signals are captured via a fake `async_dispatcher_send`
  monkey-patched onto the module under test.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.ha_switchbee.const import (
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
)
from custom_components.ha_switchbee.coordinator import SwitchBeeCoordinator
from custom_components.ha_switchbee.models import SwitchBeeDevice


class _FakeClient:
    """Minimal SwitchBeeWSClient stand-in for poll unit tests."""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.next_states: dict[int, Any] = {}
        self.get_states_calls: list[list[int]] = []

    def add_listener(self, _cb: Any) -> Any:
        return lambda: None

    async def get_multiple_states(self, ids: list[int]) -> dict[int, Any]:
        self.get_states_calls.append(list(ids))
        return dict(self.next_states)

    async def stop(self) -> None:
        self.connected = False


class _FakeHass:
    """Stand-in for HomeAssistant exposing only the bits the poll touches."""

    def __init__(self) -> None:
        self.loop = asyncio.get_event_loop()


@pytest.fixture
def fake_hass() -> _FakeHass:
    return _FakeHass()


def _build_coordinator(hass: _FakeHass, client: _FakeClient, **state: Any) -> SwitchBeeCoordinator:
    """Build a coordinator with two devices seeded into the cache."""
    devices = {
        1: SwitchBeeDevice(id=1, name="A", hw="hw", type="SWITCH", zone="Z", state=state.get(1, "OFF")),
        2: SwitchBeeDevice(id=2, name="B", hw="hw", type="SWITCH", zone="Z", state=state.get(2, "OFF")),
    }
    return SwitchBeeCoordinator(hass, client, cu_mac="a82108e7688f", devices=devices)


@pytest.fixture
def captured_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Intercept async_dispatcher_send so we can assert on signals fired."""
    calls: list[tuple[str, Any]] = []

    def fake_send(_hass: Any, signal: str, value: Any) -> None:
        calls.append((signal, value))

    # The coordinator does `from homeassistant.helpers.dispatcher import async_dispatcher_send`
    # lazily INSIDE _reconcile_once and _on_push. Patch the import target so both pick up the fake.
    import sys
    import types
    dispatcher_module = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher_module.async_dispatcher_send = fake_send  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher_module)
    return calls


@pytest.mark.asyncio
async def test_reconcile_no_drift_dispatches_nothing(
    fake_hass: _FakeHass, captured_dispatch: list[tuple[str, Any]]
) -> None:
    """When poll returns the same states the cache already has, no signals fire."""
    client = _FakeClient()
    client.next_states = {1: "OFF", 2: "OFF"}
    coordinator = _build_coordinator(fake_hass, client)

    drift = await coordinator._reconcile_once()

    assert drift == 0
    assert client.get_states_calls == [[1, 2]]
    assert captured_dispatch == []


@pytest.mark.asyncio
async def test_reconcile_drift_fires_signal_only_for_changed_items(
    fake_hass: _FakeHass, captured_dispatch: list[tuple[str, Any]]
) -> None:
    """One item drifted, the other matches: signal fires for the drifted one only."""
    client = _FakeClient()
    client.next_states = {1: "ON", 2: "OFF"}  # item 1 drifted; item 2 matches cache
    coordinator = _build_coordinator(fake_hass, client)

    drift = await coordinator._reconcile_once()

    assert drift == 1
    # The cache is updated with the fresh value.
    assert coordinator.data[1] == "ON"
    assert coordinator.data[2] == "OFF"
    # Exactly one dispatcher signal: for item 1, with the fresh value.
    assert len(captured_dispatch) == 1
    signal, value = captured_dispatch[0]
    assert signal == "ha_switchbee_push_a82108e7688f_1"
    assert value == "ON"


@pytest.mark.asyncio
async def test_reconcile_skipped_when_ws_disconnected(
    fake_hass: _FakeHass, captured_dispatch: list[tuple[str, Any]]
) -> None:
    """A disconnected WS short-circuits the poll: no GET_MULTIPLE_STATES call."""
    client = _FakeClient(connected=False)
    client.next_states = {1: "ON"}  # would drift if the poll ran
    coordinator = _build_coordinator(fake_hass, client)

    drift = await coordinator._reconcile_once()

    assert drift == 0
    assert client.get_states_calls == []
    assert captured_dispatch == []


@pytest.mark.asyncio
async def test_reconcile_no_devices_short_circuits(
    fake_hass: _FakeHass, captured_dispatch: list[tuple[str, Any]]
) -> None:
    """With zero devices the poll exits immediately without calling the CU."""
    client = _FakeClient()
    coordinator = SwitchBeeCoordinator(fake_hass, client, cu_mac="a82108e7688f", devices={})

    drift = await coordinator._reconcile_once()

    assert drift == 0
    assert client.get_states_calls == []


@pytest.mark.asyncio
async def test_reconcile_drift_also_notifies_update_listeners(
    fake_hass: _FakeHass, captured_dispatch: list[tuple[str, Any]]
) -> None:
    """CoordinatorEntity-style listeners are fired alongside per-item signals."""
    client = _FakeClient()
    client.next_states = {1: "ON", 2: "OFF"}
    coordinator = _build_coordinator(fake_hass, client)
    listener_calls: list[None] = []
    coordinator.async_add_listener(lambda: listener_calls.append(None))

    await coordinator._reconcile_once()

    # One drift -> exactly one listener invocation.
    assert listener_calls == [None]


def test_start_poll_task_with_zero_interval_does_not_create_task(fake_hass: _FakeHass) -> None:
    """Passing 0 disables the poll entirely."""
    client = _FakeClient()
    coordinator = _build_coordinator(fake_hass, client)
    coordinator.async_start_poll_task(0)
    assert coordinator._poll_task is None


def test_start_poll_task_clamps_below_minimum(fake_hass: _FakeHass) -> None:
    """Passing 5 (below MIN) clamps up to MIN_POLL_INTERVAL_SECONDS but still starts."""
    client = _FakeClient()
    coordinator = _build_coordinator(fake_hass, client)
    coordinator.async_start_poll_task(5)
    assert coordinator._poll_task is not None
    # The clamp itself is verified through the log; we cannot easily
    # introspect the asyncio.sleep argument without timing. Cancel for cleanup.
    coordinator._poll_task.cancel()


def test_start_poll_task_clamps_above_maximum(fake_hass: _FakeHass) -> None:
    """Passing a huge value clamps down to MAX_POLL_INTERVAL_SECONDS."""
    client = _FakeClient()
    coordinator = _build_coordinator(fake_hass, client)
    coordinator.async_start_poll_task(MAX_POLL_INTERVAL_SECONDS * 10)
    assert coordinator._poll_task is not None
    coordinator._poll_task.cancel()


def test_start_poll_task_within_range_starts_cleanly(fake_hass: _FakeHass) -> None:
    """A value inside [MIN, MAX] starts the task as requested."""
    client = _FakeClient()
    coordinator = _build_coordinator(fake_hass, client)
    coordinator.async_start_poll_task(MIN_POLL_INTERVAL_SECONDS + 1)
    assert coordinator._poll_task is not None
    coordinator._poll_task.cancel()


@pytest.mark.asyncio
async def test_start_poll_task_is_idempotent(fake_hass: _FakeHass) -> None:
    """Calling twice replaces the task (OptionsFlow path).

    `Task.cancel()` only schedules cancellation; the task reaches a
    cancelled state on the next loop iteration. Yield once with
    `asyncio.sleep(0)` so the first task actually settles before we
    assert on its terminal state.
    """
    client = _FakeClient()
    coordinator = _build_coordinator(fake_hass, client)
    coordinator.async_start_poll_task(60)
    first_task = coordinator._poll_task
    assert first_task is not None
    coordinator.async_start_poll_task(120)
    second_task = coordinator._poll_task
    assert second_task is not None
    assert second_task is not first_task
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()
    second_task.cancel()
    await asyncio.sleep(0)
