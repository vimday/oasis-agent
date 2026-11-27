"""Independently timestamp one bounded Ethereum subscription and bind it to HTTP H/C."""

import sqlite3
import ssl
import time
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Condition, Event, Thread

import certifi
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

from ..models.common import canonical_json, normalize_hash32, sha256_hex
from ..rpc.budget import BudgetPolicy, RpcBudget
from ..rpc.protocol import response_envelope, strict_json_object
from .base_newheads import (
    CHAIN_REQUEST,
    MAX_FRAME_BYTES,
    SUB_REQUEST,
    SocketFactory,
    WebSocket,
    _read,
    _write,
)
from .rpc_validation import rpc_quantity, strict_integer, strict_object, strict_string
from .state_trie import prague_header

ENDPOINT = "wss://eth.drpc.org"
DIRECTORY = "ethereum-newheads"
POLICIES = (BudgetPolicy("drpc_eth_newheads", 3, 0),)
MAX_FRAMES = 16
WINDOW_SECONDS = 55
READY_SECONDS = 12
CHILD_JOIN_SECONDS = 5
_READ_POLL_SECONDS = 0.25
_MAX_AGE_NS = 40_000_000_000


def witness_plan() -> dict[str, object]:
    return {
        "source": ENDPOINT,
        "chain_id": 1,
        "anonymous_usd_max": 0,
        "policy": asdict(POLICIES[0]),
        "reserve_for": ("connection", "eth_chainId", "eth_subscribe(newHeads)"),
        "window_seconds": WINDOW_SECONDS,
        "ready_seconds": READY_SECONDS,
        "child_join_seconds": CHILD_JOIN_SECONDS,
        "max_frames": MAX_FRAMES,
        "max_frame_bytes": MAX_FRAME_BYTES,
        "retry": False,
    }


def _socket() -> AbstractContextManager[WebSocket]:
    return connect(
        ENDPOINT,
        proxy=None,
        ssl=ssl.create_default_context(cafile=certifi.where()),
        open_timeout=5,
        close_timeout=1,
        max_size=MAX_FRAME_BYTES,
        max_queue=16,
        compression=None,
        ping_interval=None,
    )


@dataclass(frozen=True, slots=True)
class WitnessHead:
    height: int
    block_hash: str
    parent_hash: str


@dataclass(frozen=True, slots=True)
class WireHead:
    head: WitnessHead
    monotonic_ns: int
    frame_sha256: str


@dataclass(frozen=True, slots=True)
class ChildWitness:
    status: str
    discovery_monotonic_ns: int | None = None
    child_monotonic_ns: int | None = None
    discovery_frame_sha256: str | None = None
    child_frame_sha256: str | None = None


def _notification(raw: bytes, token: str, mono: int, unix: int) -> WireHead:
    value = strict_json_object(raw)
    params = strict_object(value.get("params"), "Ethereum newHeads params")
    if (
        set(value) != {"jsonrpc", "method", "params"}
        or value.get("jsonrpc") != "2.0"
        or value.get("method") != "eth_subscription"
        or set(params) != {"subscription", "result"}
        or params["subscription"] != token
    ):
        raise ValueError("invalid Ethereum newHeads notification")
    header = strict_object(params["result"], "Ethereum newHeads header")
    height = rpc_quantity(header.get("number"))
    block_hash = normalize_hash32(strict_string(header.get("hash"), "header hash"))
    block, _ = prague_header(header, height, block_hash)
    parent = normalize_hash32(strict_string(header.get("parentHash"), "parent hash"))
    timestamp = block.ref.timestamp
    if parent == block_hash or timestamp is None:
        raise ValueError("Ethereum newHeads header identity invalid")
    if not -1_000_000_000 <= unix - timestamp * 10**9 <= _MAX_AGE_NS:
        raise ValueError("Ethereum newHeads header clock or identity invalid")
    return WireHead(WitnessHead(height, block_hash, parent), mono, sha256_hex(raw))


def _accept(previous: WireHead | None, current: WireHead) -> bool:
    if previous is None:
        return True
    if current.monotonic_ns <= previous.monotonic_ns:
        raise ValueError("Ethereum newHeads clocks did not advance")
    if current.head == previous.head:
        return False
    if (
        current.head.height != previous.head.height + 1
        or current.head.parent_hash != previous.head.block_hash
    ):
        raise ValueError("Ethereum newHeads height or parent continuity failed")
    return True


def _control(raw: bytes, index: int) -> str | None:
    reply = response_envelope(raw, index)
    if "error" in reply:
        error = strict_object(reply["error"], "Ethereum witness RPC error")
        code = strict_integer(error.get("code"), "Ethereum witness RPC error code")
        raise ValueError(f"ethereum_witness_rpc_{code}")
    result = reply["result"]
    if index == 1:
        if rpc_quantity(result) != 1:
            raise ValueError("Ethereum newHeads chain is not Ethereum")
        return None
    token = strict_string(result, "Ethereum newHeads subscription ID")
    if not 1 <= len(token) <= 128:
        raise ValueError("invalid Ethereum newHeads subscription ID")
    return token


def _retain(folder: Path, name: str, raw: bytes, mono: int, unix: int) -> None:
    if not isinstance(raw, bytes) or len(raw) > MAX_FRAME_BYTES:
        raise ValueError("invalid Ethereum newHeads frame size")
    _write(folder / f"{name}.raw", raw)
    _write(
        folder / f"{name}.json",
        canonical_json({"monotonic_ns": mono, "unix_ns": unix, "sha256": sha256_hex(raw)}),
    )


def _retained(folder: Path, name: str) -> tuple[bytes, int, int]:
    raw = _read(folder / f"{name}.raw")
    meta = strict_json_object(_read(folder / f"{name}.json"))
    mono = strict_integer(meta.get("monotonic_ns"), "Ethereum witness monotonic time")
    unix = strict_integer(meta.get("unix_ns"), "Ethereum witness wall time")
    if (
        mono <= 0
        or unix <= 0
        or set(meta) != {"monotonic_ns", "unix_ns", "sha256"}
        or meta.get("sha256") != sha256_hex(raw)
    ):
        raise ValueError("Ethereum witness raw frame identity differs")
    return raw, mono, unix


class EthereumChildSession:
    def __init__(
        self,
        directory: Path,
        scope_digest: str,
        abort_http: Event,
        *,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        directory.mkdir()
        self.directory = directory
        self.abort_http = abort_http
        self.ledger = directory / "original-budget.sqlite3"
        self._factory = socket_factory or _socket
        self._stop = Event()
        self._condition = Condition()
        self._ready = Event()
        self._ack = False
        self._finished = False
        self._failure: str | None = None
        self._received: dict[int, WitnessHead] = {}
        self._frames = 0
        self._thread = Thread(target=self._worker, name="ethereum-child-feed", daemon=True)
        with RpcBudget(self.ledger, POLICIES):
            pass
        _write(
            directory / "plan.json",
            canonical_json({**witness_plan(), "scope_sha256": scope_digest}),
        )

    def start(self) -> None:
        self._started_ns = time.monotonic_ns()
        self._deadline_ns = self._started_ns + WINDOW_SECONDS * 10**9
        self._thread.start()
        if not self._ready.wait(READY_SECONDS):
            self._fail("subscription_ready_deadline")

    @property
    def failure(self) -> str | None:
        return self._failure

    def _fail(self, reason: str) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = reason
                self.abort_http.set()
            self._stop.set()
            self._ready.set()
            self._condition.notify_all()

    def _worker(self) -> None:
        try:
            self._capture()
        except InvalidStatus as exc:
            _write(
                self.directory / "upgrade.response.raw", bytes(exc.response.body)[:MAX_FRAME_BYTES]
            )
            self._fail(f"upgrade_http_{exc.response.status_code}")
        except (OSError, ValueError, TypeError, WebSocketException, sqlite3.Error) as exc:
            self._fail(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)

    def _reserve(self, budget: RpcBudget) -> None:
        def check() -> None:
            if (
                self._stop.is_set()
                or self.abort_http.is_set()
                or time.monotonic_ns() >= self._deadline_ns
            ):
                raise ValueError("Ethereum witness scope stopped")

        if budget.reserve(POLICIES[0].source, now_ms=time.time_ns() // 1_000_000, admission=check):
            raise ValueError("unexpected Ethereum witness source pacing")

    def _capture(self) -> None:
        with RpcBudget(self.ledger, POLICIES, require_existing=True) as budget:
            self._reserve(budget)
            with self._factory() as socket:
                token = None
                for index, request in ((1, CHAIN_REQUEST), (2, SUB_REQUEST)):
                    self._reserve(budget)
                    _write(self.directory / f"control-{index}.request.json", request)
                    socket.send(request, text=True)
                    raw = socket.recv(timeout=4, decode=False)
                    _retain(
                        self.directory, f"control-{index}", raw, time.monotonic_ns(), time.time_ns()
                    )
                    token = _control(raw, index)
                if token is None:
                    raise ValueError("missing Ethereum witness subscription")
                with self._condition:
                    if time.monotonic_ns() - self._started_ns > READY_SECONDS * 10**9:
                        raise ValueError("subscription_ready_deadline")
                    self._ack = True
                    self._ready.set()
                self._heads(socket, token)

    def _heads(self, socket: WebSocket, token: str) -> None:
        previous = None
        last_mono = 0
        while not self._stop.is_set() and not self.abort_http.is_set():
            if self._frames == MAX_FRAMES:
                raise ValueError("Ethereum witness frame ceiling")
            if time.monotonic_ns() >= self._deadline_ns:
                self._fail("subscription_observation_deadline")
                return
            try:
                raw = socket.recv(timeout=_READ_POLL_SECONDS, decode=False)
            except TimeoutError:
                continue
            mono, unix = time.monotonic_ns(), time.time_ns()
            self._frames += 1
            _retain(self.directory, f"head-{self._frames:02d}", raw, mono, unix)
            if mono >= self._deadline_ns:
                raise ValueError("subscription_observation_deadline")
            if mono <= last_mono:
                raise ValueError("Ethereum witness frame clock regressed")
            last_mono = mono
            current = _notification(raw, token, mono, unix)
            if _accept(previous, current):
                previous = current
                with self._condition:
                    self._received[current.head.height] = current.head
                    self._condition.notify_all()

    def close(self, child: WitnessHead | None = None) -> None:
        if self._finished:
            return
        if child is not None:
            with self._condition:
                self._condition.wait_for(
                    lambda: child.height in self._received or self._failure is not None,
                    timeout=CHILD_JOIN_SECONDS,
                )
                if child.height not in self._received and self._failure is None:
                    self._failure = "child_join_deadline"
        self._stop.set()
        self._thread.join(timeout=6)
        if self._thread.is_alive():
            self._fail("reader_shutdown_deadline")
            return
        self._finished = True
        with RpcBudget(self.ledger, POLICIES, read_only=True) as budget:
            counts = budget.counts()
        _write(
            self.directory / "STOP.json",
            canonical_json(
                {
                    "failure": self._failure,
                    "frames": self._frames,
                    "ready": self._ack,
                    "started_monotonic_ns": self._started_ns,
                    "closed_monotonic_ns": time.monotonic_ns(),
                    "counts": counts,
                }
            ),
        )


def inspect_witness(
    directory: Path,
    scope_digest: str,
    *,
    discovery: WitnessHead | None,
    discovery_request_ns: int | None,
    child: WitnessHead | None,
) -> tuple[ChildWitness, dict[str, object]]:
    """Use only retained originals, even when computing the live report."""
    expected = canonical_json({**witness_plan(), "scope_sha256": scope_digest})
    if _read(directory / "plan.json") != expected:
        raise ValueError("Ethereum witness plan mismatch")
    stop = strict_json_object(_read(directory / "STOP.json"))
    with RpcBudget(directory / "original-budget.sqlite3", POLICIES, read_only=True) as budget:
        counts = budget.counts()
    frames = strict_integer(stop.get("frames"), "Ethereum witness frame count")
    began = strict_integer(stop.get("started_monotonic_ns"), "Ethereum witness start")
    closed = strict_integer(stop.get("closed_monotonic_ns"), "Ethereum witness end")
    if stop.get("counts") != counts or not 0 <= frames <= MAX_FRAMES or not 0 < began <= closed:
        raise ValueError("Ethereum witness ledger or frame count differs")
    failure = stop.get("failure")
    if failure is not None:
        return ChildWitness(strict_string(failure, "Ethereum witness failure")), stop
    if (
        stop.get("ready") is not True
        or counts != {POLICIES[0].source: 3}
        or _read(directory / "control-1.request.json") != CHAIN_REQUEST
        or _read(directory / "control-2.request.json") != SUB_REQUEST
    ):
        raise ValueError("Ethereum witness subscription evidence is incomplete")
    chain, chain_mono, _ = _retained(directory, "control-1")
    ack, ack_mono, _ = _retained(directory, "control-2")
    _control(chain, 1)
    token = _control(ack, 2)
    if token is None or not began < chain_mono < ack_mono <= began + READY_SECONDS * 10**9:
        raise ValueError("Ethereum witness control replies are out of order")
    accepted = {}
    previous = None
    last_mono = ack_mono
    for index in range(1, frames + 1):
        raw, mono, unix = _retained(directory, f"head-{index:02d}")
        if not last_mono < mono <= closed or mono >= began + WINDOW_SECONDS * 10**9:
            raise ValueError("Ethereum witness frame outside the monotonic window")
        last_mono = mono
        row = _notification(raw, token, mono, unix)
        if _accept(previous, row):
            previous = row
            accepted[row.head.height] = row
    if discovery is None or discovery_request_ns is None or child is None:
        return ChildWitness("http_h_or_c_unavailable"), stop
    if not ack_mono < discovery_request_ns:
        return ChildWitness("subscription_not_ready_before_h_request"), stop
    a, b = accepted.get(discovery.height), accepted.get(child.height)
    if a is None or b is None:
        return ChildWitness("h_or_c_missing_from_independent_feed"), stop
    if a.head != discovery or b.head != child or child.height != discovery.height + 1:
        return ChildWitness("h_or_c_diverges_from_http"), stop
    if b.head.parent_hash != a.head.block_hash or not a.monotonic_ns < b.monotonic_ns:
        return ChildWitness("h_to_c_not_contiguous"), stop
    return ChildWitness(
        "verified", a.monotonic_ns, b.monotonic_ns, a.frame_sha256, b.frame_sha256
    ), stop
