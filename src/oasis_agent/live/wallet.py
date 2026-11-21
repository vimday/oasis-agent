"""Retained EOA wallet snapshots for action-bound cash reconciliation."""

import json
from dataclasses import asdict, dataclass

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    decode_header,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..execution.account_capture import SessionFactory
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from ..rpc.transport import RpcExchange
from .acquisition import acquire_pair
from .config import LiveConfig

USDC_IMPL_SLOT = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"


@dataclass(frozen=True, slots=True)
class WalletScope:
    chain_id: int
    account: str
    token: str
    token_decimals: int
    implementation_word: str

    def __post_init__(self) -> None:
        if self.chain_id != 8453 or type(self.chain_id) is not int:
            raise ValueError("wallet cash adapter is Base-only")
        if type(self.token_decimals) is not int or self.token_decimals != 6:
            raise ValueError("wallet cash adapter requires six-decimal USDC")
        for name in ("account", "token"):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
        object.__setattr__(self, "implementation_word", normalize_hash32(self.implementation_word))

    @classmethod
    def from_config(cls, config: LiveConfig) -> WalletScope:
        return cls(
            config.chain_id,
            config.account,
            config.pool.currency1,
            config.decimals1,
            config.expected_pins["usdc_implementation"],
        )


@dataclass(frozen=True, slots=True)
class WalletBalance:
    header: BlockSnapshot
    nonce: int
    native: int
    token: int
    token_runtime_hash: str


CONTINUITY_HALT = "live account nonce or block continuity unresolved"


def require_wallet_continuity(
    anchor: WalletBalance, current: WalletBalance, *, allow_same_block: bool = False
) -> None:
    """Compare already replayed wallets for one unchanged account/policy scope.

    No acquisition or ancestry proof is performed. Callers must independently
    bind both wallets to their account, asset and declared provider pair. Exact
    same-block replay is optional; otherwise height and time must both advance.
    """
    before, after = anchor.header.ref, current.header.ref
    if (
        type(allow_same_block) is not bool
        or any(type(n) is not int or n < 0 for n in (anchor.nonce, current.nonce))
        or before.chain_id != after.chain_id
        or anchor.nonce != current.nonce
    ):
        raise ValueError(CONTINUITY_HALT)
    if allow_same_block and anchor == current:
        return
    if (
        after.number <= before.number
        or before.timestamp is None
        or after.timestamp is None
        or after.timestamp <= before.timestamp
        or (after.number == before.number + 1 and current.header.parent_hash != before.block_hash)
    ):
        raise ValueError(CONTINUITY_HALT)


def wallet_requests(scope: WalletScope, block: BlockRef) -> list[tuple[str, list[object]]]:
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    return [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getCode", [scope.account, pin]),
        ("eth_getTransactionCount", [scope.account, pin]),
        ("eth_getBalance", [scope.account, pin]),
        ("eth_getCode", [scope.token, pin]),
        ("eth_getStorageAt", [scope.token, USDC_IMPL_SLOT, pin]),
        ("eth_call", [{"to": scope.token, "data": "0x313ce567"}, pin]),
        (
            "eth_call",
            [
                {
                    "to": scope.token,
                    "data": "0x70a08231" + encode(["address"], [scope.account]).hex(),
                },
                pin,
            ],
        ),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]


def _opening_header(
    scope: WalletScope, block: BlockRef, chain: object, raw_header: object
) -> BlockSnapshot:
    if block.chain_id != scope.chain_id or rpc_quantity(chain) != scope.chain_id:
        raise ValueError("wallet provider chain mismatch")
    header = decode_header(raw_header, chain_id=scope.chain_id)
    if header.ref != block:
        raise ValueError("wallet block changed")
    return header


def uint_word(value: object) -> int:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError("wallet balance must be a canonical ABI word")
    return int(decode(["uint256"], bytes.fromhex(value[2:]))[0])


@dataclass(frozen=True, slots=True)
class WalletCapture:
    scope: WalletScope
    block: BlockRef
    source: str
    exchanges: tuple[RpcExchange, ...]

    def reconstruct(self) -> WalletBalance:
        if (
            sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("wallet evidence exceeds byte budget")
        v = successful_results(self.exchanges, wallet_requests(self.scope, self.block))
        header = _opening_header(self.scope, self.block, v[0], v[1])
        if decode_header(v[-1], chain_id=self.scope.chain_id) != header:
            raise ValueError("wallet block changed")
        if v[2] != "0x":
            raise ValueError("wallet reconciliation requires an undelegated EOA")
        nonce, native, token = rpc_quantity(v[3]), rpc_quantity(v[4]), uint_word(v[8])
        if nonce >= 2**64 or native >= 2**256:
            raise ValueError("wallet balance/nonce overflow")
        if v[6] != self.scope.implementation_word or uint_word(v[7]) != self.scope.token_decimals:
            raise ValueError("wallet token implementation or decimals changed")
        if not isinstance(v[5], str) or not v[5].startswith("0x") or len(v[5]) <= 2:
            raise ValueError("wallet token runtime missing")
        code = bytes.fromhex(v[5][2:])
        return WalletBalance(header, nonce, native, token, "0x" + keccak(code).hex())

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.live-wallet.v1",
                "scope": asdict(self.scope),
                "balance": asdict(self.reconstruct()),
                "source": self.source,
                "execution_eligible": False,
            }
        )


def capture_wallet(
    store: EvidenceStore,
    config: LiveConfig,
    block: BlockRef,
    *,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[EvidenceRef, EvidenceRef]:
    scope = WalletScope.from_config(config)
    if block.chain_id != scope.chain_id:
        raise ValueError("wallet provider chain mismatch")
    pair = (sessions(sources[0]), sessions(sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != sources:
        raise ValueError("wallet source aliases changed")
    calls = wallet_requests(scope, block)
    opening = calls[:2]

    def verify_opening() -> None:
        headers = [
            _opening_header(scope, block, *successful_results(recorder.exchanges, opening))
            for _, recorder, _ in pair
        ]
        if headers[0] != headers[1]:
            raise ValueError("wallet providers disagree")

    acquire_pair(pair, calls, checkpoint=(len(opening), verify_opening))
    captures = []
    for _transport, recorder, source in pair:
        captures.append(WalletCapture(scope, block, source, recorder.exchanges))
    if captures[0].reconstruct() != captures[1].reconstruct():
        raise ValueError("wallet providers disagree")
    refs = [
        store.put(
            "live_wallet",
            c.payload,
            c.block,
            c.source,
            request=wire_envelope(c.exchanges, response=False),
            raw_response=wire_envelope(c.exchanges, response=True),
            interpretation={"execution_eligible": False},
        )
        for c in captures
    ]
    return refs[0], refs[1]


def replay_wallet(store: EvidenceStore, ref: EvidenceRef) -> WalletCapture:
    record = store.get_record(ref)
    if ref.kind != "live_wallet" or ref.block is None or record.request_payload is None:
        raise ValueError("wallet evidence scope missing")
    data = json.loads(record.payload)
    capture = WalletCapture(
        WalletScope(**data["scope"]),
        ref.block,
        ref.source,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload:
        raise ValueError("wallet interpretation differs from raw proof")
    return capture
