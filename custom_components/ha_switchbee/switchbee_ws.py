"""Pure asyncio SwitchBee Central Unit WebSocket protocol client.

Clean-room Python port of the protocol implemented in the JS source at
`/Users/nitaybz/Projects/homebridge-switchbee/SwitchBee/`. This module is
deliberately HA-free: it imports nothing from `homeassistant`, so it can
be run standalone, unit-tested under a plain Python 3.12 venv, and
exercised against the in-process fake CU server in `tests/fake_cu.py`.

Anti-bug: the original JS implementation has a class of bug in
`websocketApi.js:168` where `eventEmitter.off(name)` is called without
a listener argument on LOGIN timeout, which raises `TypeError` in Node
and caused nightly crashes. This Python implementation structurally
prevents that bug class by using per-command `asyncio.Future`s stored
in a `dict` keyed by `commandId`, with `dict.pop(id, None)` for cleanup.
There is no listener bookkeeping by name; there is nothing to "remove"
incorrectly. See Decision #2 in the implementation plan for context.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import re
from collections.abc import Callable
from typing import Any

import aiohttp

from .const import (
    CU_MAC_FIELD,
    CU_RESPONSE_DATA_KEY,
    DEFAULT_PORT,
    LOGIN_TIMEOUT_SECONDS,
    PER_COMMAND_TIMEOUT_SECONDS,
    RECONNECT_BACKOFF_CAP_SECONDS,
    RECONNECT_BACKOFF_INITIAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "CUMACMissingError",
    "CommandTimeoutError",
    "PushEvent",
    "SwitchBeeConnectionLost",
    "SwitchBeeProtocolError",
    "SwitchBeeWSClient",
    "normalize_cu_mac",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SwitchBeeProtocolError(Exception):
    """Base class for all SwitchBee protocol-level errors."""


class CUMACMissingError(SwitchBeeProtocolError):
    """Raised when GET_CONFIGURATION response lacks a usable `mac` field.

    Decision #15: there is NO host-based fallback. The integration must
    fail loudly so config-flow can show `cannot_connect` to the user.
    """


class CommandTimeoutError(SwitchBeeProtocolError):
    """Raised when a command does not get a reply within the timeout."""


class SwitchBeeConnectionLost(SwitchBeeProtocolError):
    """Raised on in-flight commands when the WS connection is lost."""


class InvalidTokenError(SwitchBeeProtocolError):
    """Raised internally when the CU returns INVALID_TOKEN.

    Surfaced to the caller only if a re-LOGIN retry also fails.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")


def normalize_cu_mac(raw: str | None) -> str:
    """Normalize the CU mac string into 12 lowercase hex characters.

    Verified live 2026-05-12: the CU returns `data.mac` as a 6-octet
    hyphen-separated uppercase MAC, for example `"A8-21-08-E7-68-8F"`.
    Tolerates colon separators too in case firmware drifts. REFUSES
    to fall back to a host-derived string (Decision #15) because doing
    so would invalidate the P3 unique_id disjointness invariant.

    Raises:
        CUMACMissingError: if `raw` is None, empty, or not 12 hex chars
            after separator stripping.
    """
    if raw is None or not isinstance(raw, str) or not raw:
        raise CUMACMissingError("CU did not return a `mac` field in GET_CONFIGURATION.data")
    stripped = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if not _MAC_HEX_RE.match(stripped):
        raise CUMACMissingError(f"CU returned a malformed mac: {raw!r}")
    return stripped


# ---------------------------------------------------------------------------
# Push event payload
# ---------------------------------------------------------------------------


class PushEvent:
    """A CONFIGURATION_CHANGE push notification from the CU.

    `value` carries the new state. The CU has been observed to use both
    `newValue` and `data` keys in the wild; this class accepts either
    and exposes a single `value` attribute. `id` is the item id (int).
    `name` is the human-readable device name. `known` is True if the
    item id was present in the last GET_CONFIGURATION response, False
    otherwise (the latter signals a stale config that should trigger
    a refresh in the coordinator).
    """

    __slots__ = ("id", "name", "value", "known", "raw")

    def __init__(
        self,
        *,
        item_id: int,
        name: str,
        value: Any,
        known: bool,
        raw: dict,
    ) -> None:
        self.id = item_id
        self.name = name
        self.value = value
        self.known = known
        self.raw = raw

    def __repr__(self) -> str:
        return (
            f"PushEvent(id={self.id!r}, name={self.name!r}, "
            f"value={self.value!r}, known={self.known})"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SwitchBeeWSClient:
    """Async WebSocket client for the SwitchBee Central Unit protocol.

    Concurrency model:
    * One reader task owns `ws.receive()`. It dispatches replies by
      `commandId` to per-command `asyncio.Future`s stored in
      `self._pending: dict[int, Future]`.
    * Push notifications (`notificationType=="CONFIGURATION_CHANGE"`)
      are dispatched to every registered listener.
    * On disconnect, every pending future is rejected with
      `SwitchBeeConnectionLost`. The `_pending` dict is then cleared.
    * On LOGIN timeout, the WS is closed, the reader is cancelled, and
      a reconnect supervisor backs off (1s, 2s, 4s, ..., capped) and
      retries. There is no listener-by-name removal anywhere in this
      module; the homebridge bug class is structurally absent.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        session: aiohttp.ClientSession,
        port: int = DEFAULT_PORT,
        login_timeout: float = LOGIN_TIMEOUT_SECONDS,
        command_timeout: float = PER_COMMAND_TIMEOUT_SECONDS,
        backoff_initial: float = RECONNECT_BACKOFF_INITIAL_SECONDS,
        backoff_cap: float = RECONNECT_BACKOFF_CAP_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._session = session
        self._login_timeout = login_timeout
        self._command_timeout = command_timeout
        self._backoff_initial = backoff_initial
        self._backoff_cap = backoff_cap

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._command_id_counter = itertools.count(1)
        self._token: str | None = None
        self._token_expiration_ms: int | None = None
        self._login_lock = asyncio.Lock()
        self._known_ids: set[int] = set()
        self._listeners: list[Callable[[PushEvent], None]] = []
        self._connected_event = asyncio.Event()
        self._stop_requested = False
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True if the WS is open AND the LOGIN handshake has succeeded."""
        return self._ws is not None and not self._ws.closed and self._token is not None

    @property
    def url(self) -> str:
        return f"ws://{self._host}:{self._port}"

    async def start(self) -> None:
        """Open the WS, perform LOGIN, and start the reader + supervisor.

        Returns when the first successful LOGIN completes. If LOGIN
        times out, the supervisor keeps retrying with exponential
        backoff until either `stop()` is called or LOGIN succeeds.
        """
        self._stop_requested = False
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        await self._connected_event.wait()

    async def stop(self) -> None:
        """Cancel the reader and supervisor, close the WS, drain pending."""
        self._stop_requested = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor_task
            self._supervisor_task = None
        await self._teardown_connection(reason="stop requested")

    def add_listener(self, cb: Callable[[PushEvent], None]) -> Callable[[], None]:
        """Register a callback for CONFIGURATION_CHANGE push events.

        Returns an unsubscribe function. The implementation uses a flat
        list and `list.remove(cb)`; there is no name-keyed bookkeeping
        and no equivalent of `EventEmitter.off(name)` without an arg.
        """
        self._listeners.append(cb)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(cb)

        return _unsubscribe

    async def get_configuration(self) -> dict:
        """Send GET_CONFIGURATION. Returns the parsed `data` payload."""
        data = await self._request("GET_CONFIGURATION")
        # Cache known ids so push notifications can be flagged known/unknown.
        zones = data.get("zones", []) if isinstance(data, dict) else []
        new_known: set[int] = set()
        for zone in zones:
            for item in zone.get("items", []):
                if "id" in item:
                    new_known.add(int(item["id"]))
        self._known_ids = new_known
        return data

    async def get_multiple_states(self, ids: list[int]) -> dict[int, Any]:
        """Send GET_MULTIPLE_STATES. Returns {id: state}."""
        raw = await self._request("GET_MULTIPLE_STATES", ids)
        result: dict[int, Any] = {}
        if isinstance(raw, list):
            for entry in raw:
                if "id" in entry:
                    result[int(entry["id"])] = entry.get("state")
        return result

    async def operate(self, item_id: int, value: Any) -> dict:
        """Send OPERATE with directive=SET, itemId, value."""
        params = {"directive": "SET", "itemId": int(item_id), "value": value}
        return await self._request("OPERATE", params)

    # ------------------------------------------------------------------
    # Internal: supervisor + connection lifecycle
    # ------------------------------------------------------------------

    async def _supervisor_loop(self) -> None:
        """Keep the WS connected. Back off exponentially on failure."""
        backoff = self._backoff_initial
        while not self._stop_requested:
            try:
                await self._connect_and_login()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("WS connect/login failed: %s", err)
                await self._teardown_connection(reason=f"connect/login error: {err}")
                if self._stop_requested:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_cap)
                continue

            # Connected. Reset backoff and wait for the reader to exit.
            backoff = self._backoff_initial
            try:
                if self._reader_task is not None:
                    await self._reader_task
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("WS reader exited with error: %s", err)
            finally:
                await self._teardown_connection(reason="reader exited")

            if self._stop_requested:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_cap)

    async def _connect_and_login(self) -> None:
        """Open the WS and perform LOGIN. Raises if either step fails."""
        ws = await self._session.ws_connect(self.url, heartbeat=None)
        self._ws = ws
        self._reader_task = asyncio.create_task(self._reader_loop(ws))

        try:
            login_data = await asyncio.wait_for(self._login_raw(), timeout=self._login_timeout)
        except TimeoutError as err:
            _LOGGER.warning(
                "LOGIN timed out after %.1fs; closing WS and reconnecting",
                self._login_timeout,
            )
            # P2: close the WS so the reader exits cleanly. Do NOT call
            # any per-command listener-removal helper; there are none.
            await self._teardown_connection(reason="LOGIN timeout")
            raise SwitchBeeConnectionLost("LOGIN timeout") from err
        except Exception:
            await self._teardown_connection(reason="LOGIN failed")
            raise

        self._token = login_data.get("token")
        self._token_expiration_ms = login_data.get("expiration")
        self._connected_event.set()
        _LOGGER.debug("WS LOGIN succeeded; token cached")

    async def _login_raw(self) -> dict:
        """Send LOGIN and return parsed `data` payload."""
        params = {"username": self._username, "password": self._password}
        return await self._send_and_wait("LOGIN", params, include_token=False)

    async def _teardown_connection(self, *, reason: str) -> None:
        """Reject all pending futures, cancel reader, close WS."""
        if self._pending:
            _LOGGER.debug("Rejecting %d pending command(s) due to: %s", len(self._pending), reason)
            for command_id in list(self._pending.keys()):
                fut = self._pending.pop(command_id, None)
                if fut is not None and not fut.done():
                    fut.set_exception(SwitchBeeConnectionLost(reason))
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._connected_event.clear()
        # Token is preserved across normal reconnects (the CU's token cache
        # is keyed by user and survives the WS lifecycle). But if the WS
        # bounced because of INVALID_TOKEN we will have already cleared it.

    # ------------------------------------------------------------------
    # Internal: reader
    # ------------------------------------------------------------------

    async def _reader_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read from the WS and dispatch by commandId or notificationType."""
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch_text(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Reader loop exited with: %s", err)

    def _dispatch_text(self, text: str) -> None:
        """Parse one JSON frame and route to a future or push listener."""
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            _LOGGER.warning("Could not decode CU frame: %r", text)
            return

        if not isinstance(message, dict):
            return

        if "commandId" in message:
            command_id = message["commandId"]
            fut = self._pending.pop(command_id, None)
            if fut is not None and not fut.done():
                fut.set_result(message)
            return

        if message.get("notificationType") == "CONFIGURATION_CHANGE":
            self._dispatch_push(message)

    def _dispatch_push(self, message: dict) -> None:
        """Build a PushEvent and call every listener; swallow listener errors."""
        # Accept both `newValue` and `data` shapes (Patterns: SwitchBee push
        # events use one or the other depending on firmware path).
        if "newValue" in message and message["newValue"] is not None:
            value = message["newValue"]
        else:
            value = message.get("data")
        try:
            item_id = int(message.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            _LOGGER.warning("CONFIGURATION_CHANGE without integer id: %r", message)
            return
        name = str(message.get("name", ""))
        event = PushEvent(
            item_id=item_id,
            name=name,
            value=value,
            known=item_id in self._known_ids,
            raw=message,
        )
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception as err:  # pragma: no cover - listener bug
                _LOGGER.exception("Push listener raised: %s", err)

    # ------------------------------------------------------------------
    # Internal: command sending
    # ------------------------------------------------------------------

    async def _request(self, command: str, params: Any = None) -> Any:
        """Send a command that requires a token; handle INVALID_TOKEN retry.

        Returns the `data` field of the OK reply.
        """
        try:
            reply = await self._send_and_wait(command, params, include_token=True)
        except InvalidTokenError:
            # P5: clear cached token, re-LOGIN under a lock, retry once.
            _LOGGER.info("CU returned INVALID_TOKEN; clearing cache and re-LOGIN")
            async with self._login_lock:
                self._token = None
                self._token_expiration_ms = None
                login_data = await asyncio.wait_for(self._login_raw(), timeout=self._login_timeout)
                self._token = login_data.get("token")
                self._token_expiration_ms = login_data.get("expiration")
            reply = await self._send_and_wait(command, params, include_token=True)
        return reply

    async def _send_and_wait(
        self,
        command: str,
        params: Any,
        *,
        include_token: bool,
    ) -> Any:
        """Allocate a commandId, send the frame, await reply, return `data`.

        On reply:
          status == "OK"        -> return `data`
          status == "INVALID_TOKEN" or status containing "TOKEN"
                                -> raise InvalidTokenError (caller retries)
          anything else         -> raise SwitchBeeProtocolError(status)
        """
        if self._ws is None or self._ws.closed:
            raise SwitchBeeConnectionLost("WS is not connected")

        command_id = next(self._command_id_counter)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[command_id] = fut

        frame: dict[str, Any] = {"commandId": command_id, "command": command}
        if include_token:
            if self._token is None:
                # Should never happen on the normal path because start()
                # awaits LOGIN before resolving. Safety net only.
                self._pending.pop(command_id, None)
                raise SwitchBeeConnectionLost("No token available for command")
            frame["token"] = self._token
        if params is not None:
            frame["params"] = params

        try:
            async with self._send_lock:
                await self._ws.send_str(json.dumps(frame))
        except Exception:
            self._pending.pop(command_id, None)
            raise

        try:
            message = await asyncio.wait_for(fut, timeout=self._command_timeout)
        except TimeoutError as err:
            # P2-class safety: drop the future cleanly. No name-keyed
            # listener bookkeeping; just `dict.pop(id, None)`.
            self._pending.pop(command_id, None)
            raise CommandTimeoutError(
                f"No reply for command {command!r} (commandId={command_id}) "
                f"within {self._command_timeout:.1f}s"
            ) from err

        status = message.get("status")
        if status == "OK":
            return message.get("data")
        if isinstance(status, str) and "TOKEN" in status:
            raise InvalidTokenError(status)
        raise SwitchBeeProtocolError(f"CU rejected {command!r}: {message!r}")


# Public re-exports for ergonomics. Plan Decision #4: keep `mac` field
# name and response-key in const.py so changes to either are auditable.
assert CU_MAC_FIELD == "mac"  # noqa: S101 - sanity check at import time
assert CU_RESPONSE_DATA_KEY == "data"  # noqa: S101 - sanity check at import time
