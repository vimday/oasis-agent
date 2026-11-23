"""One bounded PublicNode newHeads session with independently replayable raw frames."""

import os
import sqlite3
import ssl
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Literal, Protocol

import certifi
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

from ..errors import RpcError
from ..models.common import canonical_json, sha256_hex
from ..rpc.budget import BudgetPolicy, RpcBudget
from ..rpc.protocol import response_envelope, strict_json_object
from .base_denim import BASE_CHAIN_ID
from .base_head_compare import BaseHead, decode_notification
from .rpc_validation import rpc_quantity, strict_integer, strict_string

DIRECTORY = "base_newheads"
ENDPOINT = "wss://base-rpc.publicnode.com"
MAX_HEADS = 64
MAX_FRAME_BYTES = 128 * 1024
WS_TIMEOUT_S = 4.0
POLICIES = (BudgetPolicy("publicnode_newheads", 3, 0),)
CHAIN_REQUEST = canonical_json({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
SUB_REQUEST = canonical_json(
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "eth_subscribe",
        "params": ["newHeads"],
    }
)


class WebSocket(Protocol):
    def recv(self, *, timeout: float, decode: Literal[False]) -> bytes: ...
    def send(self, message: bytes, *, text: bool) -> None: ...


type SocketFactory = Callable[[], AbstractContextManager[WebSocket]]


def public_socket() -> AbstractContextManager[WebSocket]:
    return connect(
        ENDPOINT,
        proxy=None,
        ssl=ssl.create_default_context(cafile=certifi.where()),
        open_timeout=WS_TIMEOUT_S,
        close_timeout=1,
        max_size=MAX_FRAME_BYTES,
        max_queue=16,
        compression=None,
        ping_interval=None,
    )


class NewHeadsError(RpcError):
    def __init__(self, reason: str, *, canonical_conflict: bool = False) -> None:
        super().__init__(reason)
        self.canonical_conflict = canonical_conflict


@dataclass(frozen=True, slots=True)
class HeadObservation:
    frame: int
    frame_sha256: str
    head: BaseHead
    received_monotonic_ns: int
    received_unix_ns: int

    def to_wire(self) -> dict[str, object]:
        return asdict(self)


def _write(path: Path, raw: bytes) -> None:
    with path.open("xb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FRAME_BYTES:
        raise ValueError(f"missing or unbounded Base newHeads evidence: {path.name}")
    return path.read_bytes()


def _reply(raw: bytes, req_id: int) -> object:
    decoded = response_envelope(raw, req_id)
    if "error" in decoded:
        raise NewHeadsError("Base newHeads control request refused")
    return decoded["result"]


def _subscription(chain: bytes, ack: bytes) -> str:
    if rpc_quantity(_reply(chain, 1)) != BASE_CHAIN_ID:
        raise NewHeadsError("Base newHeads chain ID differs from 8453")
    token = strict_string(_reply(ack, 2), "subscription ID")
    if not 1 <= len(token) <= 128:
        raise ValueError("invalid Base newHeads subscription ID")
    return token


def _direct(previous: BaseHead | None, current: BaseHead) -> bool:
    if previous is None:
        return True
    if current.height == previous.height:
        if current.agrees(previous):
            return False
        raise NewHeadsError("Base newHeads same-height conflict", canonical_conflict=True)
    if current.height < previous.height:
        raise NewHeadsError("Base newHeads height regressed")
    if current.height != previous.height + 1:
        raise NewHeadsError("Base newHeads gap between observed heights")
    if current.parent_hash != previous.block_hash:
        raise NewHeadsError("Base newHeads successor parent conflicts", canonical_conflict=True)
    return True


def _frame_paths(folder: Path, index: int) -> tuple[Path, Path]:
    prefix = folder / f"frame-{index:08d}"
    return prefix.with_suffix(".raw"), prefix.with_suffix(".json")


def _frame_limit(ticks: int) -> int:
    return 2 * ticks + 16


class NewHeadsSession:
    """Timestamp application receive independently of the campaign processing loop."""

    def __init__(
        self,
        directory: Path,
        ticks: int,
        stop_state: Event,
        *,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        if type(ticks) is not int or not 1 <= ticks <= MAX_HEADS:
            raise ValueError(f"Base newHeads requires 1..{MAX_HEADS} ticks")
        directory.mkdir()
        self.directory, self.ticks, self.stop_state = directory, ticks, stop_state
        self.factory = socket_factory or public_socket
        self.ledger = directory / "original-budget.sqlite3"
        with RpcBudget(self.ledger, POLICIES):
            pass
        _write(
            directory / "plan.json",
            canonical_json(
                {
                    "schema": "oasis.base-newheads.v1",
                    "chain_id": BASE_CHAIN_ID,
                    "source": ENDPOINT,
                    "mode": "injected" if socket_factory is not None else "public-shadow",
                    "ticks": ticks,
                    "rpc_scope": ["connection", "eth_chainId", "eth_subscribe(newHeads)"],
                }
            ),
        )
        self._stop = Event()
        self._lock = Lock()
        self._queue: Queue[HeadObservation | None] = Queue(maxsize=ticks + 1)
        self._thread = Thread(target=self._read_worker, name="base-newheads", daemon=True)
        self._failure: NewHeadsError | None = None

    def start(self) -> None:
        self._thread.start()

    def counts(self) -> dict[str, int]:
        with RpcBudget(self.ledger, POLICIES, read_only=True) as budget:
            return budget.counts()

    def _fail(self, error: NewHeadsError) -> None:
        with self._lock:
            if self._failure is not None or self._stop.is_set():
                return
            self._failure = error
            self.stop_state.set()
            self._stop.set()
            _write(
                self.directory / "STOP.json",
                canonical_json(
                    {
                        "reason": str(error),
                        "canonical_conflict": error.canonical_conflict,
                        "stopped_monotonic_ns": time.monotonic_ns(),
                    }
                ),
            )
            self._queue.put_nowait(None)

    @property
    def failure(self) -> NewHeadsError | None:
        return self._failure

    def invalidate(self, error: NewHeadsError) -> None:
        self._fail(error)

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def next_head(self) -> HeadObservation:
        self.check()
        try:
            observed = self._queue.get(timeout=4 * WS_TIMEOUT_S + 1)
        except Empty:
            self._fail(NewHeadsError("Base newHeads receive deadline elapsed"))
            self.check()
            raise NewHeadsError("Base newHeads closed before another head") from None
        self.check()
        if observed is None:
            raise NewHeadsError("Base newHeads closed before another head")
        return observed

    def close(self) -> None:
        self._stop.set()
        if self._thread.ident is None:
            return
        self._thread.join(timeout=4 * WS_TIMEOUT_S + 1)
        if self._thread.is_alive():
            raise NewHeadsError("Base newHeads reader failed to stop")

    def _recv(self, socket: WebSocket) -> tuple[bytes, int, int]:
        raw = socket.recv(timeout=WS_TIMEOUT_S, decode=False)
        mono, unix = time.monotonic_ns(), time.time_ns()
        if not isinstance(raw, bytes) or len(raw) > MAX_FRAME_BYTES:
            raise NewHeadsError("invalid Base newHeads wire frame")
        return raw, mono, unix

    def _reserve(self, budget: RpcBudget) -> None:
        if self._stop.is_set() or self.stop_state.is_set():
            raise NewHeadsError("Base newHeads scope stopped")
        if budget.reserve(POLICIES[0].source, now_ms=time.time_ns() // 1_000_000):
            raise NewHeadsError("Base newHeads source pacing refused")

    def _read_worker(self) -> None:
        try:
            with RpcBudget(self.ledger, POLICIES, require_existing=True) as budget:
                self._reserve(budget)
                with self.factory() as socket:
                    for name, request in (("chain", CHAIN_REQUEST), ("subscription", SUB_REQUEST)):
                        self._reserve(budget)
                        _write(self.directory / f"{name}.request.json", request)
                        socket.send(request, text=True)
                        raw, _, _ = self._recv(socket)
                        _write(self.directory / f"{name}.response.json", raw)
                        if name == "chain" and rpc_quantity(_reply(raw, 1)) != BASE_CHAIN_ID:
                            raise NewHeadsError("Base newHeads chain ID differs from 8453")
                    subscription = _subscription(
                        _read(self.directory / "chain.response.json"),
                        _read(self.directory / "subscription.response.json"),
                    )
                    self._frames(socket, subscription)
        except InvalidStatus as exc:
            _write(
                self.directory / "upgrade.response.raw", bytes(exc.response.body)[:MAX_FRAME_BYTES]
            )
            self._fail(NewHeadsError(f"Base newHeads upgrade HTTP {exc.response.status_code}"))
        except NewHeadsError as exc:
            self._fail(exc)
        except (
            OSError,
            sqlite3.Error,
            TimeoutError,
            TypeError,
            ValueError,
            WebSocketException,
        ) as exc:
            self._fail(NewHeadsError(f"Base newHeads refused input: {type(exc).__name__}"))

    def _frames(self, socket: WebSocket, subscription: str) -> None:
        previous = None
        accepted = 0
        for index in range(1, _frame_limit(self.ticks) + 1):
            if self._stop.is_set() or self.stop_state.is_set():
                return
            raw, mono, unix = self._recv(socket)
            raw_path, info_path = _frame_paths(self.directory, index)
            digest = sha256_hex(raw)
            _write(raw_path, raw)
            _write(
                info_path,
                canonical_json(
                    {
                        "received_monotonic_ns": mono,
                        "received_unix_ns": unix,
                        "sha256": digest,
                    }
                ),
            )
            head = decode_notification(strict_json_object(raw), subscription)
            if _direct(previous, head):
                self._queue.put_nowait(HeadObservation(index, digest, head, mono, unix))
                previous = head
                accepted += 1
                if accepted == self.ticks:
                    return
        raise NewHeadsError("Base newHeads frame limit exhausted")


def replay_head_observation(directory: Path, reference: object) -> HeadObservation:
    if not isinstance(reference, dict):
        raise ValueError("missing Base newHeads reference")
    plan = strict_json_object(_read(directory / "plan.json"))
    ticks = strict_integer(plan.get("ticks"), "Base newHeads tick count")
    if (
        plan.get("schema") != "oasis.base-newheads.v1"
        or plan.get("source") != ENDPOINT
        or plan.get("chain_id") != BASE_CHAIN_ID
        or not 1 <= ticks <= MAX_HEADS
    ):
        raise ValueError("invalid Base newHeads replay plan")
    stop = directory / "STOP.json"
    if stop.exists() and strict_json_object(_read(stop)).get("canonical_conflict") is not False:
        raise ValueError("Base newHeads capture has a canonical conflict")
    index = strict_integer(reference.get("frame"), "Base newHeads frame index")
    if not 1 <= index <= _frame_limit(ticks):
        raise ValueError("Base newHeads frame index outside capture")
    with RpcBudget(directory / "original-budget.sqlite3", POLICIES, read_only=True) as budget:
        if budget.counts() != {POLICIES[0].source: 3}:
            raise ValueError("Base newHeads source ledger disagrees with capture")
    if (
        _read(directory / "chain.request.json") != CHAIN_REQUEST
        or _read(directory / "subscription.request.json") != SUB_REQUEST
    ):
        raise ValueError("Base newHeads control request bytes changed")
    subscription = _subscription(
        _read(directory / "chain.response.json"),
        _read(directory / "subscription.response.json"),
    )
    previous = None
    accepted = last_mono = 0
    observed = None
    for cursor in range(1, index + 1):
        raw_path, info_path = _frame_paths(directory, cursor)
        raw, info = _read(raw_path), strict_json_object(_read(info_path))
        digest = sha256_hex(raw)
        mono = strict_integer(info.get("received_monotonic_ns"), "head receive time")
        unix = strict_integer(info.get("received_unix_ns"), "head Unix time")
        if digest != info.get("sha256") or not last_mono <= mono or mono <= 0 or unix <= 0:
            raise ValueError("Base newHeads frame digest or clock changed")
        last_mono = mono
        head = decode_notification(strict_json_object(raw), subscription)
        if _direct(previous, head):
            previous = head
            accepted += 1
            observed = HeadObservation(cursor, digest, head, mono, unix)
    if not 1 <= accepted <= ticks or observed is None or observed.to_wire() != reference:
        raise ValueError("Base newHeads observation differs from state manifest")
    return observed
