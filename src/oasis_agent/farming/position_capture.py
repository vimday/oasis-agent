"""Evidence-bound position snapshots; no signing or economic promotion authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from ..evidence.rpc_validation import decode_exchange, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, canonical_json, normalize_address, normalize_hex
from ..rpc.transport import RpcExchange
from .position_rpc import PositionCallRpc, RpcPositionStateSource
from .positions import PositionSnapshot, read_position_snapshot


class PositionSnapshotRpc(PositionCallRpc, Protocol):
    def get_block_snapshot(self, block: int | str = "latest") -> BlockSnapshot: ...


def collect_position_snapshot(
    rpc: PositionSnapshotRpc,
    *,
    position_manager: str,
    state_view: str,
    token_id: int,
    block_number: int,
) -> tuple[BlockSnapshot, PositionSnapshot]:
    """Use a fresh recorded RPC session, including its initial chain-ID exchange."""
    if type(block_number) is not int or block_number < 0:
        raise ValueError("position capture requires an explicit nonnegative block number")
    header = rpc.get_block_snapshot(block_number)
    if header.ref.number != block_number or header.ref.chain_id != rpc.chain_id():
        raise ValueError("position capture block or chain mismatch")
    snapshot = read_position_snapshot(
        position_manager,
        token_id,
        header.ref,
        RpcPositionStateSource(rpc, position_manager, state_view),
    )
    if rpc.get_block_snapshot(block_number) != header:
        raise ValueError("position capture block changed during collection")
    return header, snapshot


class _CapturedCalls:
    """Replay only exact successful hash-pinned calls; never fall back to a network."""

    def __init__(self, header: BlockSnapshot, exchanges: tuple[RpcExchange, ...]) -> None:
        self.block = header.ref
        self.calls: dict[tuple[str, str], str] = {}
        seen_ids: set[int] = set()
        chain_seen = False
        header_positions: list[int] = []
        call_positions: list[int] = []
        for index, exchange in enumerate(exchanges):
            params, response = decode_exchange(exchange)
            if exchange.request_id in seen_ids:
                raise ValueError("position capture repeats a request ID")
            seen_ids.add(exchange.request_id)
            if exchange.method not in {"eth_chainId", "eth_getBlockByNumber", "eth_call"}:
                raise ValueError("unexpected method in position capture")
            if "error" in response:
                continue
            result = response["result"]
            if exchange.method == "eth_chainId":
                if params or rpc_quantity(result) != self.block.chain_id:
                    raise ValueError("position capture chain ID mismatch")
                chain_seen = True
            elif exchange.method == "eth_getBlockByNumber":
                if not chain_seen:
                    raise ValueError("position capture header precedes chain ID")
                if (
                    len(params) != 2
                    or params[0] != hex(self.block.number)
                    or params[1] is not False
                    or not isinstance(result, dict)
                    or rpc_quantity(result.get("number")) != self.block.number
                    or result.get("hash") != self.block.block_hash
                    or result.get("parentHash") != header.parent_hash
                    or rpc_quantity(result.get("timestamp")) != self.block.timestamp
                ):
                    raise ValueError("position capture header mismatch")
                header_positions.append(index)
            else:
                if not header_positions:
                    raise ValueError("position call precedes opening header")
                if len(params) != 2 or not isinstance(params[0], dict):
                    raise ValueError("invalid position call parameters")
                call, pin = params
                assert isinstance(call, dict)
                if set(call) != {"to", "data"}:
                    raise ValueError("position call cannot include overrides or ambient context")
                if (
                    not isinstance(pin, dict)
                    or set(pin) != {"blockHash", "requireCanonical"}
                    or pin["blockHash"] != self.block.block_hash
                    or pin["requireCanonical"] is not True
                ):
                    raise ValueError("position call must be canonical hash-pinned")
                if not isinstance(call["to"], str) or not isinstance(call["data"], str):
                    raise ValueError("invalid position call address/calldata")
                if not isinstance(result, str):
                    raise ValueError("position call result must be hex bytes")
                key = normalize_address(call["to"]), normalize_hex(call["data"])
                if key in self.calls:
                    raise ValueError("position capture repeats a successful call")
                self.calls[key] = normalize_hex(result)
                call_positions.append(index)
        if (
            not chain_seen
            or len(header_positions) < 2
            or not call_positions
            or header_positions[-1] < call_positions[-1]
        ):
            raise ValueError("position capture requires successful chain/opening/calls/closing")

    def chain_id(self) -> int:
        return self.block.chain_id

    def call(self, to: str, data: str, block: int | str | BlockRef = "latest") -> str:
        if block != self.block:
            raise ValueError("position reconstruction requested another block")
        key = normalize_address(to), normalize_hex(data)
        if key not in self.calls:
            raise ValueError("position capture is missing an exact contract/calldata response")
        return self.calls.pop(key)


@dataclass(frozen=True, slots=True)
class PositionSnapshotCapture:
    source: str
    header: BlockSnapshot
    state_view: str
    snapshot: PositionSnapshot
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("position capture source label is required")
        object.__setattr__(self, "state_view", normalize_address(self.state_view))
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        self.validate()

    def validate(self) -> None:
        if self.snapshot.block != self.header.ref:
            raise ValueError("position snapshot does not match captured header")
        rpc = _CapturedCalls(self.header, self.exchanges)
        recomputed = read_position_snapshot(
            self.snapshot.position_manager,
            self.snapshot.token_id,
            self.header.ref,
            RpcPositionStateSource(rpc, self.snapshot.position_manager, self.state_view),
        )
        if rpc.calls:
            raise ValueError("position capture contains unrelated successful calls")
        if recomputed != self.snapshot:
            raise ValueError("position snapshot does not match raw call reconstruction")

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.position-snapshot.v1",
                "header": asdict(self.header),
                "state_view": self.state_view,
                "snapshot": asdict(self.snapshot),
            }
        )


def persist_position_snapshot_evidence(
    store: EvidenceStore, capture: PositionSnapshotCapture, *, retrieved_at: str | None = None
) -> EvidenceRef:
    capture.validate()
    return store.put(
        "position_snapshot",
        capture.payload,
        capture.snapshot.block,
        capture.source,
        request={
            "encoding": "hex_exact_bytes",
            "exchanges": [
                {
                    "method": item.method,
                    "request_id": item.request_id,
                    "attempt": item.attempt,
                    "payload": item.request_payload.hex(),
                }
                for item in capture.exchanges
            ],
        },
        raw_response={
            "encoding": "hex_exact_bytes",
            "exchanges": [
                {
                    "method": item.method,
                    "request_id": item.request_id,
                    "attempt": item.attempt,
                    "payload": item.response_payload.hex(),
                }
                for item in capture.exchanges
            ],
        },
        interpretation={
            "schema": "oasis.position-snapshot-evidence.v1",
            "validation": "raw_rpc_reconstruction",
            "deployment_identity_verified": False,
            "execution_eligible": False,
        },
        retrieved_at=retrieved_at,
        schema_version=1,
    )


__all__ = [
    "PositionSnapshotCapture",
    "PositionSnapshotRpc",
    "collect_position_snapshot",
    "persist_position_snapshot_evidence",
]
