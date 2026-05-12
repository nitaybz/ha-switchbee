"""In-process asyncio WebSocket fake of the SwitchBee Central Unit.

Speaks the same JSON-over-WS protocol as the real CU, on the local
loopback at an ephemeral port. Used by `tests/test_switchbee_ws.py`
to drive the SwitchBeeWSClient through happy paths and failure modes
without touching a real device.

The fake supports configurable failure modes via the `mode` attribute
or per-instance flags:
    - mode="normal"               default; LOGIN succeeds, all commands OK
    - mode="never_reply_login"    LOGIN frames are read but no reply is sent
    - mode="invalid_token"        first OPERATE replies INVALID_TOKEN, then OK
    - mode="drop_connection_mid"  drop the WS after the first OPERATE arrives
    - mode="missing_mac"          GET_CONFIGURATION returns data without `mac`

The fake records every received frame in `self.received` so tests can
introspect the wire shape. It also exposes `push_configuration_change()`
to fire a synthetic notification at the client.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

# Verified live 2026-05-12 against Moshe's CU at 192.168.68.57.
SAMPLE_MAC = "A8-21-08-E7-68-8F"
SAMPLE_NAME = "SwitchBee"
SAMPLE_VERSION = "1.6.10"
SAMPLE_CU_CODE = "SB-FAKE"
SAMPLE_LAST_CONF_CHANGE = 1715500000000

SAMPLE_ZONES = [
    {
        "name": "Living Room",
        "items": [
            {"id": 3, "name": "Pictures", "type": "SWITCH"},
            {"id": 7, "name": "Ceiling", "type": "DIMMER"},
            {"id": 12, "name": "Blind 2", "type": "SHUTTER"},
        ],
    },
    {
        "name": "Kitchen",
        "items": [
            {"id": 21, "name": "Counter", "type": "SWITCH"},
        ],
    },
]

FAKE_TOKEN = "fake-token-abc123"


class FakeCU:
    """An asyncio WebSocket server that pretends to be a SwitchBee CU.

    Use as an async context manager:

        async with FakeCU() as cu:
            client = SwitchBeeWSClient("127.0.0.1", "u", "p", port=cu.port, session=s)
            ...

    Or manually:

        cu = FakeCU()
        await cu.start()
        try:
            ...
        finally:
            await cu.stop()
    """

    def __init__(
        self,
        *,
        mode: str = "normal",
        username: str = "user",
        password: str = "pass",
        omit_mac: bool = False,
    ) -> None:
        self.mode = mode
        self.username = username
        self.password = password
        self.omit_mac = omit_mac
        self._server: websockets.WebSocketServer | None = None
        self.port: int = 0
        self.received: list[dict] = []
        self._connections: set[WebSocketServerProtocol] = set()
        self._invalid_token_consumed = False
        self._drop_connection_consumed = False
        # Number of LOGIN attempts to reject with no reply before switching
        # to normal-mode replies. Tests can flip this on the fly.
        self._login_reject_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> FakeCU:
        await self.start()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        # websockets.serve returns the underlying asyncio server via .sockets.
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        # Close active connections first so the client sees a clean EOF.
        for ws in list(self._connections):
            with contextlib.suppress(Exception):
                await ws.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------------
    # Public helpers for tests
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Switch failure mode at runtime (used by reconnect tests)."""
        self.mode = mode

    async def push_configuration_change(
        self,
        *,
        item_id: int,
        name: str,
        value: Any,
        shape: str = "newValue",
    ) -> None:
        """Send an unsolicited CONFIGURATION_CHANGE to every connection.

        `shape` is either "newValue" or "data" to exercise both push
        payload shapes the CU has been observed to send.
        """
        if shape == "newValue":
            message = {
                "notificationType": "CONFIGURATION_CHANGE",
                "id": item_id,
                "name": name,
                "newValue": value,
            }
        elif shape == "data":
            message = {
                "notificationType": "CONFIGURATION_CHANGE",
                "id": item_id,
                "name": name,
                "data": value,
            }
        else:
            raise ValueError(f"Unknown push shape: {shape!r}")
        encoded = json.dumps(message)
        for ws in list(self._connections):
            with contextlib.suppress(Exception):
                await ws.send(encoded)

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _handler(self, websocket: WebSocketServerProtocol) -> None:
        self._connections.add(websocket)
        try:
            async for raw in websocket:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.received.append(frame)
                await self._handle_frame(websocket, frame)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)

    async def _handle_frame(self, ws: WebSocketServerProtocol, frame: dict) -> None:
        command = frame.get("command")
        command_id = frame.get("commandId")
        params = frame.get("params")

        if command == "LOGIN":
            await self._reply_login(ws, command_id, params)
            return

        if command == "GET_CONFIGURATION":
            await self._reply_get_configuration(ws, command_id)
            return

        if command == "GET_MULTIPLE_STATES":
            await self._reply_get_multiple_states(ws, command_id, params)
            return

        if command == "OPERATE":
            await self._reply_operate(ws, command_id, params)
            return

        # Unknown command -> generic error.
        await self._send_ok(ws, command_id, data=None)

    # ------------------------------------------------------------------
    # Command replies
    # ------------------------------------------------------------------

    async def _reply_login(
        self,
        ws: WebSocketServerProtocol,
        command_id: int,
        params: Any,
    ) -> None:
        if self.mode == "never_reply_login":
            # Silently swallow the LOGIN frame.
            return
        if self._login_reject_count > 0:
            self._login_reject_count -= 1
            return
        if (
            not isinstance(params, dict)
            or params.get("username") != self.username
            or params.get("password") != self.password
        ):
            await self._send_status(ws, command_id, status="INVALID_CREDENTIALS")
            return
        # Expiration is ms-since-epoch, ~10 minutes in the future.
        expiration_ms = int((time.time() + 600) * 1000)
        await self._send_ok(
            ws,
            command_id,
            data={"token": FAKE_TOKEN, "expiration": expiration_ms},
        )

    async def _reply_get_configuration(
        self,
        ws: WebSocketServerProtocol,
        command_id: int,
    ) -> None:
        data: dict[str, Any] = {
            "name": SAMPLE_NAME,
            "version": SAMPLE_VERSION,
            "cuCode": SAMPLE_CU_CODE,
            "lastConfChange": SAMPLE_LAST_CONF_CHANGE,
            "zones": SAMPLE_ZONES,
        }
        if self.mode == "missing_mac" or self.omit_mac:
            # Intentionally omit mac to drive the CUMACMissingError path.
            pass
        else:
            data["mac"] = SAMPLE_MAC
        await self._send_ok(ws, command_id, data=data)

    async def _reply_get_multiple_states(
        self,
        ws: WebSocketServerProtocol,
        command_id: int,
        params: Any,
    ) -> None:
        ids = params if isinstance(params, list) else []
        states = [{"id": item_id, "state": "OFF"} for item_id in ids]
        await self._send_ok(ws, command_id, data=states)

    async def _reply_operate(
        self,
        ws: WebSocketServerProtocol,
        command_id: int,
        params: Any,
    ) -> None:
        if self.mode == "invalid_token" and not self._invalid_token_consumed:
            self._invalid_token_consumed = True
            await self._send_status(ws, command_id, status="INVALID_TOKEN")
            return
        if self.mode == "drop_connection_mid" and not self._drop_connection_consumed:
            self._drop_connection_consumed = True
            await ws.close()
            return
        item_id = params.get("itemId") if isinstance(params, dict) else None
        value = params.get("value") if isinstance(params, dict) else None
        await self._send_ok(ws, command_id, data={"id": item_id, "state": value})

    # ------------------------------------------------------------------
    # Wire helpers
    # ------------------------------------------------------------------

    async def _send_ok(
        self,
        ws: WebSocketServerProtocol,
        command_id: int,
        *,
        data: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "commandId": command_id,
            "status": "OK",
        }
        if data is not None:
            payload["data"] = data
        await ws.send(json.dumps(payload))

    async def _send_status(
        self,
        ws: WebSocketServerProtocol,
        command_id: int,
        *,
        status: str,
    ) -> None:
        await ws.send(json.dumps({"commandId": command_id, "status": status}))


__all__ = ["FakeCU", "FAKE_TOKEN", "SAMPLE_MAC", "SAMPLE_ZONES"]


# Silence unused-import false positive in tooling that imports only the
# Callable annotation type without using it elsewhere.
_ = Callable
