"""WebSocket hints wake a scan; reconnect gaps are covered by persisted HTTP logs."""

import json
import ssl
from threading import Event, Thread
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import certifi
from websockets.sync.client import connect

from .discovery import (
    AERO_CREATED,
    AERO_MINT,
    AERO_SYNC,
    CL_BURN,
    CL_MINT,
    SLIP_CREATED,
    TRANSFER,
    V3_CREATED,
    V4_INITIALIZED,
    V4_MODIFIED,
)
from .venues import AERO_FACTORY, SLIPSTREAM, V4_MANAGER


def alchemy_websocket_url(http_url: str) -> str | None:
    parts = urlsplit(http_url)
    if parts.scheme != "https" or parts.hostname != "base-mainnet.g.alchemy.com":
        # Free public endpoints have no WebSocket hint stream; polling covers them.
        return None
    return urlunsplit(("wss", parts.netloc, parts.path, parts.query, ""))


class EventWakeup:
    def __init__(self, url: str, target: str, v3_factory: str):
        self._url = url
        self._addresses = sorted({target, v3_factory, AERO_FACTORY, V4_MANAGER, *SLIPSTREAM})
        self._revision = 0
        self._stop = Event()
        self._wake = Event()
        self.connected = False
        self.last_error: str | None = None
        self.notifications = 0
        self._thread = Thread(target=self._run, daemon=True, name="launch-events")

    def start(self) -> None:
        self._thread.start()

    def add_pools(self, addresses: list[str]) -> None:
        updated = sorted(set(self._addresses) | set(addresses))
        if updated != self._addresses:
            self._addresses = updated
            self._revision += 1

    def wait(self, seconds: float) -> bool:
        result = self._wake.wait(seconds)
        self._wake.clear()
        return result

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=12)

    def _notification(self, message: Any, subscription: str) -> None:
        if not isinstance(message, dict) or message.get("method") != "eth_subscription":
            return
        params = message.get("params", {})
        if params.get("subscription") != subscription or not isinstance(params.get("result"), dict):
            return
        self.notifications += 1
        self._wake.set()

    def _run(self) -> None:
        delay = 1
        while not self._stop.is_set():
            revision = self._revision
            try:
                with connect(
                    self._url,
                    proxy=None,
                    ssl=ssl.create_default_context(cafile=certifi.where()),
                    open_timeout=8,
                    close_timeout=1,
                    max_size=1_048_576,
                ) as ws:
                    ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "eth_subscribe",
                                "params": [
                                    "logs",
                                    {
                                        "address": self._addresses,
                                        "topics": [
                                            [
                                                AERO_CREATED,
                                                V3_CREATED,
                                                SLIP_CREATED,
                                                V4_INITIALIZED,
                                                V4_MODIFIED,
                                                AERO_MINT,
                                                AERO_SYNC,
                                                CL_MINT,
                                                CL_BURN,
                                                TRANSFER,
                                            ]
                                        ],
                                    },
                                ],
                            }
                        )
                    )
                    ack = json.loads(ws.recv(timeout=8))
                    if (
                        ack.get("id") != 1
                        or not isinstance(ack.get("result"), str)
                        or ack.get("error")
                    ):
                        raise ValueError("subscription rejected")
                    self.connected = True
                    self.last_error = None
                    delay = 1
                    # Always request HTTP catch-up after (re)connection.
                    self._wake.set()
                    while not self._stop.is_set() and revision == self._revision:
                        try:
                            self._notification(json.loads(ws.recv(timeout=1)), ack["result"])
                        except TimeoutError:
                            continue
            except Exception as exc:  # noqa: BLE001
                # Exception text may include the credential URL.
                self.last_error = type(exc).__name__
                self._wake.set()
                self._stop.wait(delay)
                delay = min(delay * 2, 8)
            finally:
                self.connected = False
