"""Dual-provider, exact-wire Uniswap v4 slot0 window capture."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    RecordedSession,
    decode_address_word,
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges, strict_json_object
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    normalize_hex,
)
from ..rpc.client import RpcClient
from ..rpc.transport import RpcExchange
from ..uniswap_v4.state_rpc import Slot0State

POOL_MANAGER_SELECTOR = "0x" + keccak(b"poolManager()")[:4].hex()
GET_SLOT0_SELECTOR = "0x" + keccak(b"getSlot0(bytes32)")[:4].hex()


@dataclass(frozen=True, slots=True)
class Slot0WindowCapture:
    source: str
    headers: tuple[BlockSnapshot, ...]
    state_view: str
    pool_manager: str
    pool_id: str
    state_view_runtime_keccak: str
    pool_manager_runtime_keccak: str
    slot0: tuple[Slot0State, ...]
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.headers:
            raise ValueError("slot0 capture requires source and blocks")
        object.__setattr__(self, "state_view", normalize_address(self.state_view))
        object.__setattr__(self, "pool_manager", normalize_address(self.pool_manager))
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id))
        object.__setattr__(
            self,
            "state_view_runtime_keccak",
            normalize_hash32(self.state_view_runtime_keccak),
        )
        object.__setattr__(
            self,
            "pool_manager_runtime_keccak",
            normalize_hash32(self.pool_manager_runtime_keccak),
        )
        object.__setattr__(self, "headers", tuple(self.headers))
        object.__setattr__(self, "slot0", tuple(self.slot0))
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if len(self.headers) != len(self.slot0):
            raise ValueError("slot0 capture length mismatch")
        if len({header.ref for header in self.headers}) != len(self.headers):
            raise ValueError("slot0 capture blocks must be unique")
        expected = _expected_calls(self)
        results = successful_results(self.exchanges, expected)
        _validate_results(self, results)

    @property
    def states(self) -> tuple[Slot0State, ...]:
        return self.slot0

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.slot0-window.v1",
                "source": self.source,
                "state_view": self.state_view,
                "pool_manager": self.pool_manager,
                "pool_id": self.pool_id,
                "state_view_runtime_keccak": self.state_view_runtime_keccak,
                "pool_manager_runtime_keccak": self.pool_manager_runtime_keccak,
                "headers": [asdict(item) for item in self.headers],
                "slot0": [asdict(item) for item in self.slot0],
            }
        )


def _expected_calls(capture: Slot0WindowCapture) -> list[tuple[str, list[object]]]:
    expected: list[tuple[str, list[object]]] = [("eth_chainId", [])]
    for header in capture.headers:
        pin = {"blockHash": header.ref.block_hash, "requireCanonical": True}
        expected.extend(
            [
                ("eth_getBlockByNumber", [hex(header.ref.number), False]),
                ("eth_getCode", [capture.state_view, pin]),
                ("eth_getCode", [capture.pool_manager, pin]),
                (
                    "eth_call",
                    [{"to": capture.state_view, "data": POOL_MANAGER_SELECTOR}, pin],
                ),
                (
                    "eth_call",
                    [
                        {
                            "to": capture.state_view,
                            "data": "0x"
                            + (
                                bytes.fromhex(GET_SLOT0_SELECTOR[2:])
                                + encode(["bytes32"], [bytes.fromhex(capture.pool_id[2:])])
                            ).hex(),
                        },
                        pin,
                    ],
                ),
                ("eth_getBlockByNumber", [hex(header.ref.number), False]),
            ]
        )
    return expected


def _decode_slot0(raw: object) -> Slot0State:
    if not isinstance(raw, str):
        raise ValueError("slot0 response must be hex bytes")
    normalized = normalize_hex(raw)
    if len(normalized) != 2 + 32 * 4 * 2:
        raise ValueError("slot0 response has invalid ABI length")
    sqrt_price, tick, protocol_fee, lp_fee = decode(
        ["uint160", "int24", "uint24", "uint24"], bytes.fromhex(normalized[2:])
    )
    return Slot0State(int(sqrt_price), int(tick), int(protocol_fee), int(lp_fee))


def _validate_results(capture: Slot0WindowCapture, results: tuple[object, ...]) -> None:
    if int(str(results[0]), 16) != capture.headers[0].ref.chain_id:
        raise ValueError("slot0 chain mismatch")
    offset = 1
    reconstructed: list[Slot0State] = []
    for header in capture.headers:
        opening, state_code, manager_code, manager_result, slot_result, closing = results[
            offset : offset + 6
        ]
        offset += 6
        if (
            decode_header(opening, chain_id=header.ref.chain_id) != header
            or decode_header(closing, chain_id=header.ref.chain_id) != header
        ):
            raise ValueError("slot0 header changed")
        require_code_hash(state_code, capture.state_view_runtime_keccak)
        require_code_hash(manager_code, capture.pool_manager_runtime_keccak)
        if decode_address_word(manager_result) != capture.pool_manager:
            raise ValueError("state view pool manager getter mismatch")
        reconstructed.append(_decode_slot0(slot_result))
    if tuple(reconstructed) != capture.slot0:
        raise ValueError("slot0 interpretation differs from raw responses")


@dataclass(frozen=True, slots=True)
class Slot0WindowQuorum:
    left: Slot0WindowCapture
    right: Slot0WindowCapture

    def __post_init__(self) -> None:
        if self.left.source == self.right.source:
            raise ValueError("slot0 quorum requires distinct sources")
        if (
            self.left.headers != self.right.headers
            or self.left.state_view != self.right.state_view
            or self.left.pool_manager != self.right.pool_manager
            or self.left.pool_id != self.right.pool_id
            or self.left.state_view_runtime_keccak != self.right.state_view_runtime_keccak
            or self.left.pool_manager_runtime_keccak != self.right.pool_manager_runtime_keccak
            or self.left.slot0 != self.right.slot0
        ):
            raise ValueError("slot0 providers disagree")


def collect_slot0_window(
    session: RecordedSession,
    *,
    chain_id: int,
    state_view: str,
    pool_manager: str,
    pool_id: str,
    block_numbers: tuple[int, ...],
    state_view_runtime_keccak: str,
    pool_manager_runtime_keccak: str,
) -> Slot0WindowCapture:
    transport, recorder, source = session
    if not block_numbers or any(type(value) is not int or value < 0 for value in block_numbers):
        raise ValueError("slot0 window requires explicit nonnegative blocks")
    if tuple(sorted(set(block_numbers))) != block_numbers:
        raise ValueError("slot0 window blocks must be strictly increasing")
    client = RpcClient(transport, expected_chain_id=chain_id)
    headers: list[BlockSnapshot] = []
    values: list[Slot0State] = []
    normalized_view = normalize_address(state_view)
    normalized_manager = normalize_address(pool_manager)
    normalized_pool = normalize_hash32(pool_id)
    for number in block_numbers:
        opening = client.get_block_snapshot(number)
        pin = {"blockHash": opening.ref.block_hash, "requireCanonical": True}
        transport.request("eth_getCode", [normalized_view, pin])
        transport.request("eth_getCode", [normalized_manager, pin])
        manager_raw = transport.request(
            "eth_call", [{"to": normalized_view, "data": POOL_MANAGER_SELECTOR}, pin]
        )
        if decode_address_word(manager_raw) != normalized_manager:
            raise ValueError("state view pool manager getter mismatch")
        call_data = (
            "0x"
            + (
                bytes.fromhex(GET_SLOT0_SELECTOR[2:])
                + encode(["bytes32"], [bytes.fromhex(normalized_pool[2:])])
            ).hex()
        )
        slot_raw = transport.request("eth_call", [{"to": normalized_view, "data": call_data}, pin])
        value = _decode_slot0(slot_raw)
        if client.get_block_snapshot(number) != opening:
            raise ValueError("slot0 block changed during collection")
        headers.append(opening)
        values.append(value)
    return Slot0WindowCapture(
        source,
        tuple(headers),
        normalized_view,
        normalized_manager,
        normalized_pool,
        state_view_runtime_keccak,
        pool_manager_runtime_keccak,
        tuple(values),
        recorder.exchanges,
    )


def collect_slot0_window_quorum(
    primary: RecordedSession,
    secondary: RecordedSession,
    *,
    chain_id: int,
    state_view: str,
    pool_manager: str,
    pool_id: str,
    block_numbers: tuple[int, ...],
    state_view_runtime_keccak: str,
    pool_manager_runtime_keccak: str,
) -> Slot0WindowQuorum:
    validate_session_pair(primary, secondary)

    def collect(session: RecordedSession) -> Slot0WindowCapture:
        return collect_slot0_window(
            session,
            chain_id=chain_id,
            state_view=state_view,
            pool_manager=pool_manager,
            pool_id=pool_id,
            block_numbers=block_numbers,
            state_view_runtime_keccak=state_view_runtime_keccak,
            pool_manager_runtime_keccak=pool_manager_runtime_keccak,
        )

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="slot0-window") as workers:
        left = workers.submit(collect, primary)
        right = workers.submit(collect, secondary)
        return Slot0WindowQuorum(left.result(), right.result())


def persist_slot0_capture(
    store: EvidenceStore,
    capture: Slot0WindowCapture,
    *,
    retrieved_at: str,
) -> EvidenceRef:
    return store.put(
        "slot0_window",
        capture.payload,
        capture.headers[-1].ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={"schema": "oasis.slot0-window.v1", "execution_eligible": False},
        retrieved_at=retrieved_at,
        schema_version=1,
    )


def persist_slot0_window_evidence(
    store: EvidenceStore,
    quorum: Slot0WindowQuorum,
    *,
    retrieved_at: str | None = None,
) -> tuple[EvidenceRef, EvidenceRef]:
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    return (
        persist_slot0_capture(store, quorum.left, retrieved_at=timestamp),
        persist_slot0_capture(store, quorum.right, retrieved_at=timestamp),
    )


def _required_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _required_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _required_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def replay_slot0_window_evidence(
    store: EvidenceStore, reference: EvidenceRef
) -> Slot0WindowCapture:
    record = store.get_record(reference)
    if record.request_payload is None:
        raise ValueError("slot0 evidence lacks request bytes")
    data = strict_json_object(record.payload)
    expected = {
        "schema",
        "source",
        "state_view",
        "pool_manager",
        "pool_id",
        "state_view_runtime_keccak",
        "pool_manager_runtime_keccak",
        "headers",
        "slot0",
    }
    if set(data) != expected or data["schema"] != "oasis.slot0-window.v1":
        raise ValueError("slot0 evidence schema mismatch")
    headers: list[BlockSnapshot] = []
    for raw in _required_list(data["headers"], "headers"):
        item = _required_object(raw, "header")
        if set(item) != {"ref", "parent_hash"}:
            raise ValueError("slot0 header has invalid fields")
        ref = _required_object(item["ref"], "block ref")
        if set(ref) != {"chain_id", "number", "block_hash", "timestamp"}:
            raise ValueError("slot0 block ref has invalid fields")
        headers.append(
            BlockSnapshot(
                BlockRef(
                    _required_int(ref["chain_id"], "chain id"),
                    _required_int(ref["number"], "block number"),
                    _required_str(ref["block_hash"], "block hash"),
                    _required_int(ref["timestamp"], "block timestamp"),
                ),
                _required_str(item["parent_hash"], "parent hash"),
            )
        )
    values: list[Slot0State] = []
    for raw in _required_list(data["slot0"], "slot0"):
        item = _required_object(raw, "slot0 item")
        if set(item) != {"sqrt_price_x96", "tick", "protocol_fee", "lp_fee"}:
            raise ValueError("slot0 item has invalid fields")
        values.append(
            Slot0State(
                _required_int(item["sqrt_price_x96"], "sqrt price"),
                _required_int(item["tick"], "tick"),
                _required_int(item["protocol_fee"], "protocol fee"),
                _required_int(item["lp_fee"], "LP fee"),
            )
        )
    capture = Slot0WindowCapture(
        _required_str(data["source"], "source"),
        tuple(headers),
        _required_str(data["state_view"], "state view"),
        _required_str(data["pool_manager"], "pool manager"),
        _required_str(data["pool_id"], "pool id"),
        _required_str(data["state_view_runtime_keccak"], "state view runtime hash"),
        _required_str(data["pool_manager_runtime_keccak"], "pool manager runtime hash"),
        tuple(values),
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload or capture.headers[-1].ref != reference.block:
        raise ValueError("slot0 interpretation differs from raw reconstruction")
    return capture


__all__ = [
    "Slot0WindowCapture",
    "Slot0WindowQuorum",
    "collect_slot0_window",
    "collect_slot0_window_quorum",
    "persist_slot0_capture",
    "persist_slot0_window_evidence",
    "replay_slot0_window_evidence",
]
