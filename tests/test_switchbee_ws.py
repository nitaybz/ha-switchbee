"""Protocol tests for `SwitchBeeWSClient` against the in-process fake CU.

Covers Phase 1 acceptance scenarios:
    * LOGIN + GET_CONFIGURATION happy path (with normalized mac)
    * LOGIN timeout triggers a clean reconnect (Provable Property P2)
    * INVALID_TOKEN clears cached token and re-LOGINs (Provable Property P5)
    * OPERATE round-trip + CONFIGURATION_CHANGE push (both `newValue` and
      `data` shapes)
    * Concurrent commands resolve correctly (no commandId collision)
    * Drop-mid-command reconnect rejects the in-flight future cleanly
    * GET_CONFIGURATION returning no mac raises CUMACMissingError when
      the caller tries to normalize it

All tests use pure asyncio. No HA-shaped fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import pytest

from custom_components.ha_switchbee.switchbee_ws import (
    CUMACMissingError,
    PushEvent,
    SwitchBeeConnectionLost,
    SwitchBeeWSClient,
    normalize_cu_mac,
)
from tests.fake_cu import FAKE_TOKEN, SAMPLE_MAC, FakeCU

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# pytest-homeassistant-custom-component (added in Phase 3) installs
# `pytest_socket` and disables all real sockets by default. These tests use an
# in-process WebSocket fake CU that binds to 127.0.0.1, so they need real
# socket access. The `socket_enabled` fixture is provided by pytest_socket and
# re-enables sockets for the test that requests it.
@pytest.fixture(autouse=True)
def _enable_sockets(socket_enabled):
    """Enable real socket I/O for every test in this module."""
    yield


@pytest.fixture
async def http_session():
    """An aiohttp session scoped to a single test."""
    async with aiohttp.ClientSession() as session:
        yield session


async def _make_client(
    cu: FakeCU,
    session: aiohttp.ClientSession,
    **kwargs,
) -> SwitchBeeWSClient:
    """Construct a client pointed at the fake CU with fast timeouts."""
    return SwitchBeeWSClient(
        "127.0.0.1",
        "user",
        "pass",
        port=cu.port,
        session=session,
        login_timeout=kwargs.pop("login_timeout", 1.0),
        command_timeout=kwargs.pop("command_timeout", 1.0),
        backoff_initial=kwargs.pop("backoff_initial", 0.1),
        backoff_cap=kwargs.pop("backoff_cap", 0.4),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_login_succeeds_and_caches_token(http_session: aiohttp.ClientSession) -> None:
    async with FakeCU() as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            assert client.connected is True
            # Token was cached during start()
            assert client._token == FAKE_TOKEN  # noqa: SLF001 - intentional check
        finally:
            await client.stop()


async def test_get_configuration_returns_mac_and_zones(
    http_session: aiohttp.ClientSession,
) -> None:
    async with FakeCU() as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            data = await client.get_configuration()
            # Normalize the mac the same way the integration will.
            assert normalize_cu_mac(data["mac"]) == "a82108e7688f"
            assert isinstance(data["zones"], list)
            assert any(item["id"] == 3 for zone in data["zones"] for item in zone.get("items", []))
        finally:
            await client.stop()


async def test_operate_round_trip(http_session: aiohttp.ClientSession) -> None:
    async with FakeCU() as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            await client.get_configuration()  # prime known ids
            reply = await client.operate(3, "ON")
            assert reply == {"id": 3, "state": "ON"}
        finally:
            await client.stop()


# ---------------------------------------------------------------------------
# Push delivery (Provable Property P4 minimum)
# ---------------------------------------------------------------------------


async def test_push_delivery_accepts_both_shapes(
    http_session: aiohttp.ClientSession,
) -> None:
    """CU push events use `newValue` OR `data`; both must be parsed."""
    received: list[PushEvent] = []

    async with FakeCU() as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            await client.get_configuration()  # so id=3 is `known`
            client.add_listener(lambda evt: received.append(evt))

            await cu.push_configuration_change(
                item_id=3, name="Pictures", value="ON", shape="newValue"
            )
            await cu.push_configuration_change(item_id=7, name="Ceiling", value=42, shape="data")
            # Allow the reader task to drain.
            for _ in range(40):
                await asyncio.sleep(0.01)
                if len(received) >= 2:
                    break
            assert len(received) == 2
            by_id = {evt.id: evt for evt in received}
            assert by_id[3].value == "ON"
            assert by_id[3].known is True
            assert by_id[7].value == 42
            assert by_id[7].known is True
        finally:
            await client.stop()


async def test_unknown_id_push_is_flagged_unknown(
    http_session: aiohttp.ClientSession,
) -> None:
    """A push for an id not in the last GET_CONFIGURATION is marked unknown."""
    received: list[PushEvent] = []
    async with FakeCU() as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            await client.get_configuration()
            client.add_listener(lambda evt: received.append(evt))
            await cu.push_configuration_change(item_id=9999, name="Ghost", value="ON")
            for _ in range(40):
                await asyncio.sleep(0.01)
                if received:
                    break
            assert len(received) == 1
            assert received[0].known is False
        finally:
            await client.stop()


# ---------------------------------------------------------------------------
# Concurrent commands (commandId disjointness)
# ---------------------------------------------------------------------------


async def test_concurrent_commands_resolve_correctly(
    http_session: aiohttp.ClientSession,
) -> None:
    """10 GET_CONFIGURATION calls in flight should each resolve correctly."""
    async with FakeCU() as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            results = await asyncio.gather(*(client.get_configuration() for _ in range(10)))
            assert len(results) == 10
            for data in results:
                assert data["mac"] == SAMPLE_MAC
        finally:
            await client.stop()


# ---------------------------------------------------------------------------
# P2: LOGIN timeout triggers reconnect, NOT listener removal
# ---------------------------------------------------------------------------


async def test_login_timeout_triggers_reconnect_p2(
    http_session: aiohttp.ClientSession,
) -> None:
    """The fake CU swallows the first LOGIN. Client must time out, close the
    WS, back off, retry. Once the fake CU flips to normal mode, the client
    eventually finishes start() with `connected=True`. Crucially, no
    AttributeError / TypeError from listener cleanup should appear."""
    async with FakeCU(mode="never_reply_login") as cu:
        client = await _make_client(
            cu,
            http_session,
            login_timeout=0.3,
            backoff_initial=0.1,
            backoff_cap=0.2,
        )
        start_task = asyncio.create_task(client.start())
        # Let the first LOGIN attempt time out.
        await asyncio.sleep(0.6)
        assert not client.connected
        assert not start_task.done()
        # Flip the fake CU back to normal; client supervisor should
        # reconnect and finish start().
        cu.set_mode("normal")
        try:
            await asyncio.wait_for(start_task, timeout=3.0)
        finally:
            await client.stop()
        assert client.connected is False  # we just stopped


# ---------------------------------------------------------------------------
# P5: INVALID_TOKEN clears cached token and re-LOGINs
# ---------------------------------------------------------------------------


async def test_invalid_token_recovery_p5(http_session: aiohttp.ClientSession) -> None:
    """First OPERATE replies INVALID_TOKEN. Client must clear the cached
    token, re-LOGIN, and retry the same OPERATE. Final result: OK."""
    async with FakeCU(mode="invalid_token") as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            original_token = client._token  # noqa: SLF001
            reply = await client.operate(3, "ON")
            assert reply == {"id": 3, "state": "ON"}
            # Token was refreshed (still the same FAKE_TOKEN value, but
            # the client cleared and re-fetched).
            assert client._token == original_token  # noqa: SLF001
            # The fake CU recorded one LOGIN, one OPERATE (rejected),
            # another LOGIN, another OPERATE (OK).
            commands = [f.get("command") for f in cu.received]
            assert commands.count("LOGIN") == 2
            assert commands.count("OPERATE") == 2
        finally:
            await client.stop()


# ---------------------------------------------------------------------------
# Drop-mid-command -> in-flight future rejects cleanly (no silent hang)
# ---------------------------------------------------------------------------


async def test_drop_mid_command_rejects_future(
    http_session: aiohttp.ClientSession,
) -> None:
    """Fake CU drops the WS on the first OPERATE without replying. The
    awaiting coroutine must surface SwitchBeeConnectionLost, not hang."""
    async with FakeCU(mode="drop_connection_mid") as cu:
        client = await _make_client(cu, http_session, command_timeout=2.0)
        await client.start()
        try:
            with pytest.raises(SwitchBeeConnectionLost):
                await client.operate(3, "ON")
        finally:
            # Cancel the supervisor reconnect attempts before exiting.
            await client.stop()


# ---------------------------------------------------------------------------
# CUMACMissingError surfaces when the CU response omits `mac`
# ---------------------------------------------------------------------------


async def test_missing_mac_raises_cu_mac_missing_error(
    http_session: aiohttp.ClientSession,
) -> None:
    async with FakeCU(mode="missing_mac") as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        try:
            data = await client.get_configuration()
            assert "mac" not in data
            with pytest.raises(CUMACMissingError):
                normalize_cu_mac(data.get("mac"))
        finally:
            await client.stop()


# ---------------------------------------------------------------------------
# Test process hygiene: no listener-removal-by-name calls anywhere
# ---------------------------------------------------------------------------


def test_module_uses_dict_pop_for_command_dispatch() -> None:
    """Sanity check: the module must use the `dict.pop(id, None)` pattern
    for command bookkeeping, not a per-name listener model.

    Phase 1 contract (Decision #2 / Provable Property P2): the homebridge
    bug class is structurally absent. The Python implementation uses
    `self._pending: dict[int, asyncio.Future]` and `self._pending.pop(id)`
    on completion or timeout, never an `off(name)`-style removal of an
    event-emitter listener.
    """
    import ast
    from pathlib import Path

    src_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "ha_switchbee"
        / "switchbee_ws.py"
    )
    tree = ast.parse(src_path.read_text())

    # Walk the AST to confirm `self._pending.pop(...)` is invoked at least
    # twice (once for normal-reply path, once for the timeout / disconnect
    # cleanup path). This is the structural fix.
    pop_calls = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pop"
            and isinstance(node.func.value, ast.Attribute)
        ):
            attr_chain = node.func.value
            # self._pending.pop(...)
            if (
                attr_chain.attr == "_pending"
                and isinstance(attr_chain.value, ast.Name)
                and attr_chain.value.id == "self"
            ):
                pop_calls += 1
    assert pop_calls >= 2, f"Expected >=2 `self._pending.pop` sites, found {pop_calls}"


# ---------------------------------------------------------------------------
# Belt-and-suspenders: client refuses to send after stop()
# ---------------------------------------------------------------------------


async def test_client_refuses_send_after_stop(
    http_session: aiohttp.ClientSession,
) -> None:
    async with FakeCU() as cu:
        client = await _make_client(cu, http_session)
        await client.start()
        await client.stop()
        with pytest.raises(SwitchBeeConnectionLost):
            await client.operate(3, "ON")


# Cleanup hint: when a test fails mid-run, leftover supervisor tasks would
# normally complain about "Task was destroyed but it is pending". The
# `async with FakeCU()` + `client.stop()` pairing handles the happy path;
# this contextmanager suppresses CancelledError noise during teardown for
# tests that pytest interrupts.
@contextlib.asynccontextmanager
async def _suppress_cancel():
    with contextlib.suppress(asyncio.CancelledError):
        yield
