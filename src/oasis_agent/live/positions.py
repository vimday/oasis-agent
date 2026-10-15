"""Raw two-provider proof of the intended mint/burn position state at receipt block.

This establishes NFT/position outcome only, not owner cash settlement or NAV.
"""

import json
from dataclasses import asdict, dataclass

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..execution.account_capture import SessionFactory
from ..farming.positions import decode_position_info, position_id
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from ..models.v4 import PoolKey
from ..rpc.transport import RpcExchange
from ..uniswap_v4.pool_id import pool_id_from_key
from .config import LiveConfig


@dataclass(frozen=True, slots=True)
class PositionOutcomeScope:
    chain_id: int
    account: str
    position_manager: str
    state_view: str
    position_manager_hash: str
    state_view_hash: str
    pool_key: PoolKey
    token_id: int
    lower: int
    upper: int
    liquidity: int
    burned: bool

    def __post_init__(self) -> None:
        for name in ("account", "position_manager", "state_view"):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
        for name in ("position_manager_hash", "state_view_hash"):
            object.__setattr__(self, name, normalize_hash32(getattr(self, name)))
        if type(self.chain_id) is not int or self.chain_id <= 0 or type(self.burned) is not bool:
            raise ValueError("position outcome requires explicit chain and action")
        if type(self.liquidity) is not int or not 0 <= self.liquidity < 2**128:
            raise ValueError("position outcome liquidity is invalid")
        if (self.liquidity == 0) != self.burned:
            raise ValueError("mint needs positive liquidity and burn needs zero liquidity")
        position_id(self.position_manager, self.lower, self.upper, self.token_id)

    @property
    def pool_id(self) -> str:
        return pool_id_from_key(self.pool_key)


def _call(
    to: str, signature: str, types: list[str], args: list[object], pin: object
) -> tuple[str, list[object]]:
    data = "0x" + (keccak(signature.encode())[:4] + encode(types, args)).hex()
    return "eth_call", [{"to": to, "data": data}, pin]


def outcome_requests(
    scope: PositionOutcomeScope, block: BlockRef
) -> list[tuple[str, list[object]]]:
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    requests: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getCode", [scope.position_manager, pin]),
        ("eth_getCode", [scope.state_view, pin]),
    ]
    if scope.burned:
        requests.append(
            _call(
                scope.position_manager, "positionInfo(uint256)", ["uint256"], [scope.token_id], pin
            )
        )
    else:
        requests.extend(
            _call(scope.position_manager, signature, ["uint256"], [scope.token_id], pin)
            for signature in (
                "ownerOf(uint256)",
                "getPoolAndPositionInfo(uint256)",
                "getPositionLiquidity(uint256)",
            )
        )
    core_id = position_id(scope.position_manager, scope.lower, scope.upper, scope.token_id)
    requests.append(
        _call(
            scope.state_view,
            "getPositionLiquidity(bytes32,bytes32)",
            ["bytes32", "bytes32"],
            [bytes.fromhex(scope.pool_id[2:]), bytes.fromhex(core_id[2:])],
            pin,
        )
    )
    requests.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return requests


def _abi(types: list[str], value: object) -> tuple[object, ...]:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("position outcome is not ABI data")
    raw = bytes.fromhex(value[2:])
    result = tuple(decode(types, raw))
    if encode(types, result) != raw:
        raise ValueError("position outcome ABI is noncanonical")
    return result


@dataclass(frozen=True, slots=True)
class PositionOutcomeCapture:
    scope: PositionOutcomeScope
    block: BlockRef
    source: str
    exchanges: tuple[RpcExchange, ...]

    def validate(self) -> BlockSnapshot:
        if (
            sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("position outcome evidence exceeds byte budget")
        if self.block.chain_id != self.scope.chain_id:
            raise ValueError("position outcome chain mismatch")
        values = successful_results(self.exchanges, outcome_requests(self.scope, self.block))
        if rpc_quantity(values[0]) != self.scope.chain_id:
            raise ValueError("position outcome provider chain mismatch")
        header = decode_header(values[1], chain_id=self.scope.chain_id)
        if (
            header.ref != self.block
            or decode_header(values[-1], chain_id=self.scope.chain_id) != header
        ):
            raise ValueError("position outcome header changed")
        require_code_hash(values[2], self.scope.position_manager_hash)
        require_code_hash(values[3], self.scope.state_view_hash)
        if self.scope.burned:
            if _abi(["uint256"], values[4]) != (0,):
                raise ValueError("burned NFT position info was not cleared")
        else:
            if _abi(["address"], values[4]) != (self.scope.account,):
                raise ValueError("minted NFT owner differs from account")
            key, packed = _abi(["(address,address,uint24,int24,address)", "uint256"], values[5])
            if key != tuple(asdict(self.scope.pool_key).values()):
                raise ValueError("minted position belongs to another pool")
            if type(packed) is not int:
                raise ValueError("position info word required")
            info = decode_position_info(packed)
            if (
                info.pool_id_prefix != self.scope.pool_id[:52]
                or (info.tick_lower, info.tick_upper) != (self.scope.lower, self.scope.upper)
                or info.has_subscriber
            ):
                raise ValueError("minted position range, pool prefix or subscription mismatch")
            if _abi(["uint128"], values[6]) != (self.scope.liquidity,):
                raise ValueError("minted manager liquidity differs from intent")
        if _abi(["uint128"], values[-2]) != (self.scope.liquidity,):
            raise ValueError("core position liquidity differs from intended outcome")
        return header

    @property
    def payload(self) -> bytes:
        self.validate()
        return canonical_json(
            {
                "schema": "oasis.live-position-outcome.v1",
                "scope": asdict(self.scope),
                "block": asdict(self.block),
                "source": self.source,
                "execution_eligible": False,
            }
        )


def collect_position_outcome(
    store: EvidenceStore,
    config: LiveConfig,
    block: BlockRef,
    *,
    token_id: int,
    lower: int,
    upper: int,
    liquidity: int,
    burned: bool,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[EvidenceRef, EvidenceRef]:
    scope = PositionOutcomeScope(
        config.chain_id,
        config.account,
        config.position_manager,
        config.state_view,
        config.expected_pins["position_manager"],
        config.expected_pins["state_view"],
        PoolKey(*config.pool.tuple()),
        token_id,
        lower,
        upper,
        liquidity,
        burned,
    )
    if scope.pool_id != config.pool_id:
        raise ValueError("configured pool id differs from its key")
    pair = (sessions(sources[0]), sessions(sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != sources:
        raise ValueError("position outcome provider aliases differ from declared sources")
    captures = []
    for transport, recorder, source in pair:
        for method, params in outcome_requests(scope, block):
            transport.request(method, params)
        capture = PositionOutcomeCapture(scope, block, source, recorder.exchanges)
        capture.validate()
        captures.append(capture)
    if captures[0].validate() != captures[1].validate():
        raise ValueError("position outcome providers disagree on header")
    refs = [
        store.put(
            "live_position_outcome",
            capture.payload,
            block,
            capture.source,
            request=wire_envelope(capture.exchanges, response=False),
            raw_response=wire_envelope(capture.exchanges, response=True),
            interpretation={
                "scope": "nft_and_core_position_state",
                "execution_eligible": False,
            },
        )
        for capture in captures
    ]
    return refs[0], refs[1]


def replay_position_outcome(store: EvidenceStore, ref: EvidenceRef) -> PositionOutcomeCapture:
    record = store.get_record(ref)
    if ref.kind != "live_position_outcome" or ref.block is None or record.request_payload is None:
        raise ValueError("position outcome evidence scope missing")
    data = json.loads(record.payload)
    scope_data = dict(data["scope"])
    scope_data["pool_key"] = PoolKey(**scope_data["pool_key"])
    capture = PositionOutcomeCapture(
        PositionOutcomeScope(**scope_data),
        ref.block,
        ref.source,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload:
        raise ValueError("position outcome interpretation differs from raw proof")
    return capture
