"""Dual-provider token unit identity at one canonical decision block."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from eth_abi.abi import decode
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    RecordedSession,
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
    ZERO_ADDRESS,
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from ..rpc.client import RpcClient
from ..rpc.transport import RpcExchange

DECIMALS_SELECTOR = "0x" + keccak(b"decimals()")[:4].hex()


@dataclass(frozen=True, slots=True)
class TokenIdentityPin:
    address: str
    decimals: int
    runtime_keccak: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        if type(self.decimals) is not int or not 0 <= self.decimals <= 77:
            raise ValueError("token decimals must be an integer between 0 and 77")
        if self.address == ZERO_ADDRESS:
            if self.decimals != 18 or self.runtime_keccak is not None:
                raise ValueError("native currency requires decimals 18 and no runtime hash")
        else:
            if self.runtime_keccak is None:
                raise ValueError("ERC-20 token requires a runtime hash")
            object.__setattr__(self, "runtime_keccak", normalize_hash32(self.runtime_keccak))


@dataclass(frozen=True, slots=True)
class TokenIdentityCapture:
    source: str
    header: BlockSnapshot
    pin: TokenIdentityPin
    exchanges: tuple[RpcExchange, ...]

    @property
    def identity(self) -> TokenIdentityPin:
        return self.pin

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.token-identity.v1",
                "source": self.source,
                "header": asdict(self.header),
                "pin": asdict(self.pin),
            }
        )

    def validate(self) -> None:
        expected, pin_arg = _expected(self.header, self.pin)
        results = successful_results(self.exchanges, expected)
        if int(str(results[0]), 16) != self.header.ref.chain_id:
            raise ValueError("token identity chain mismatch")
        if decode_header(results[1], chain_id=self.header.ref.chain_id) != self.header:
            raise ValueError("token identity opening header mismatch")
        if self.pin.address == ZERO_ADDRESS:
            closing_index = 2
        else:
            assert self.pin.runtime_keccak is not None
            require_code_hash(results[2], self.pin.runtime_keccak)
            raw = results[3]
            if not isinstance(raw, str):
                raise ValueError("token decimals returned non-hex data")
            values = decode(["uint8"], bytes.fromhex(raw.removeprefix("0x")))
            if int(values[0]) != self.pin.decimals:
                raise ValueError("token decimals mismatch")
            closing_index = 4
        if decode_header(results[closing_index], chain_id=self.header.ref.chain_id) != self.header:
            raise ValueError("token identity closing header mismatch")
        if pin_arg["blockHash"] != self.header.ref.block_hash:
            raise AssertionError("unreachable token identity pin mismatch")


@dataclass(frozen=True, slots=True)
class TokenIdentityQuorum:
    left: TokenIdentityCapture
    right: TokenIdentityCapture

    def __post_init__(self) -> None:
        if self.left.source == self.right.source:
            raise ValueError("token identity quorum requires distinct sources")
        self.left.validate()
        self.right.validate()
        if self.left.header != self.right.header or self.left.pin != self.right.pin:
            raise ValueError("token identity providers disagree")


def _expected(
    header: BlockSnapshot, pin: TokenIdentityPin
) -> tuple[list[tuple[str, list[object]]], dict[str, object]]:
    block = header.ref
    pin_arg: dict[str, object] = {
        "blockHash": block.block_hash,
        "requireCanonical": True,
    }
    expected: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]
    if pin.address != ZERO_ADDRESS:
        expected.extend(
            [
                ("eth_getCode", [pin.address, pin_arg]),
                (
                    "eth_call",
                    [{"to": pin.address, "data": DECIMALS_SELECTOR}, pin_arg],
                ),
            ]
        )
    expected.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return expected, pin_arg


def _collect_one(
    session: RecordedSession,
    *,
    chain_id: int,
    block_number: int,
    pin: TokenIdentityPin,
) -> TokenIdentityCapture:
    transport, recorder, source = session
    client = RpcClient(transport, expected_chain_id=chain_id)
    opening = client.get_block_snapshot(block_number)
    if opening.ref.number != block_number:
        raise ValueError("token identity opened the wrong block")
    block_pin = {"blockHash": opening.ref.block_hash, "requireCanonical": True}
    if pin.address != ZERO_ADDRESS:
        transport.request("eth_getCode", [pin.address, block_pin])
        transport.request("eth_call", [{"to": pin.address, "data": DECIMALS_SELECTOR}, block_pin])
    if client.get_block_snapshot(block_number) != opening:
        raise ValueError("token identity block changed during collection")
    return TokenIdentityCapture(source, opening, pin, recorder.exchanges)


def collect_token_identity_quorum(
    primary: RecordedSession,
    secondary: RecordedSession,
    *,
    chain_id: int,
    block_number: int,
    pin: TokenIdentityPin,
) -> TokenIdentityQuorum:
    validate_session_pair(primary, secondary)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="token-identity") as workers:
        left = workers.submit(
            _collect_one,
            primary,
            chain_id=chain_id,
            block_number=block_number,
            pin=pin,
        )
        right = workers.submit(
            _collect_one,
            secondary,
            chain_id=chain_id,
            block_number=block_number,
            pin=pin,
        )
        return TokenIdentityQuorum(left.result(), right.result())


def _persist_token_identity_capture(
    store: EvidenceStore,
    capture: TokenIdentityCapture,
    *,
    retrieved_at: str | None,
) -> EvidenceRef:
    capture.validate()
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    return store.put(
        "token_identity",
        capture.payload,
        capture.header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={"schema": "oasis.token-identity-evidence.v1"},
        retrieved_at=timestamp,
        schema_version=1,
    )


def persist_token_identity_evidence(
    store: EvidenceStore,
    quorum: TokenIdentityQuorum,
    *,
    retrieved_at: str | None = None,
) -> tuple[EvidenceRef, EvidenceRef]:
    if quorum.left.pin != quorum.right.pin or quorum.left.header != quorum.right.header:
        raise ValueError("token identity quorum changed before persistence")
    return (
        _persist_token_identity_capture(store, quorum.left, retrieved_at=retrieved_at),
        _persist_token_identity_capture(store, quorum.right, retrieved_at=retrieved_at),
    )


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _capture_identity_from_payload(payload: bytes) -> tuple[BlockSnapshot, TokenIdentityPin]:
    data = strict_json_object(payload)
    if (
        set(data) != {"schema", "source", "header", "pin"}
        or data["schema"] != "oasis.token-identity.v1"
    ):
        raise ValueError("token identity payload has invalid fields or schema")
    header_data = _object(data["header"], "token header")
    if set(header_data) != {"ref", "parent_hash"}:
        raise ValueError("token header has invalid fields")
    ref_data = _object(header_data["ref"], "token block")
    if set(ref_data) != {"chain_id", "number", "block_hash", "timestamp"}:
        raise ValueError("token block has invalid fields")
    header = BlockSnapshot(
        BlockRef(
            _integer(ref_data["chain_id"], "chain ID"),
            _integer(ref_data["number"], "block number"),
            _string(ref_data["block_hash"], "block hash"),
            _integer(ref_data["timestamp"], "block timestamp"),
        ),
        _string(header_data["parent_hash"], "parent hash"),
    )
    pin_data = _object(data["pin"], "token pin")
    if set(pin_data) != {"address", "decimals", "runtime_keccak"}:
        raise ValueError("token pin has invalid fields")
    runtime = pin_data["runtime_keccak"]
    if runtime is not None and not isinstance(runtime, str):
        raise TypeError("token runtime hash must be a string or null")
    return header, TokenIdentityPin(
        _string(pin_data["address"], "token address"),
        _integer(pin_data["decimals"], "token decimals"),
        runtime,
    )


def replay_token_identity_evidence(
    store: EvidenceStore,
    reference: EvidenceRef,
) -> TokenIdentityCapture:
    record = store.get_record(reference)
    if reference.kind != "token_identity" or record.request_payload is None:
        raise ValueError("token identity reference has the wrong kind or missing request")
    header, pin = _capture_identity_from_payload(record.payload)
    capture = TokenIdentityCapture(
        reference.source,
        header,
        pin,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload or reference.block != header.ref:
        raise ValueError("token identity interpretation differs from raw reconstruction")
    capture.validate()
    return capture


__all__ = [
    "DECIMALS_SELECTOR",
    "TokenIdentityCapture",
    "TokenIdentityPin",
    "TokenIdentityQuorum",
    "collect_token_identity_quorum",
    "persist_token_identity_evidence",
    "replay_token_identity_evidence",
]
