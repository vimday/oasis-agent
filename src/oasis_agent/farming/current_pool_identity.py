"""Replay current finalized identity discovery without promoting a target.

Finalized is used only to resolve the opening header. Every subsequent state
read is hash pinned; historical availability and economic acceptance are separate.
"""

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import cast

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.exact_capture import decode_address_word, decode_header, successful_results
from ..evidence.rpc_validation import decode_exchange
from ..evm.proxy_indicators import inspect_proxy_indicators
from ..models.chain import BlockSnapshot
from ..models.common import ZERO_ADDRESS, canonical_json, normalize_address, normalize_hex
from ..models.v4 import PoolKey
from ..rpc.transport import RpcExchange
from ..uniswap_v4.pool_id import pool_id_from_key
from ..uniswap_v4.state_rpc import Slot0State
from .price_evidence import selector


@dataclass(frozen=True, slots=True)
class CurrentIdentityPlan:
    chain_id: int
    pool_manager: str
    state_view: str
    tokens: tuple[str, ...]
    pools: tuple[PoolKey, ...]
    providers: tuple[str, str]
    max_head_age_seconds: int

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("positive integer chain required")
        if type(self.max_head_age_seconds) is not int or not 1 <= self.max_head_age_seconds <= 1800:
            raise ValueError("bounded current research head age required")
        for role in ("pool_manager", "state_view"):
            object.__setattr__(self, role, normalize_address(getattr(self, role)))
        object.__setattr__(self, "tokens", tuple(normalize_address(t) for t in self.tokens))
        addresses = (self.pool_manager, self.state_view, *self.tokens)
        if (
            not 1 <= len(self.tokens) <= 8
            or len(set(addresses)) != len(addresses)
            or ZERO_ADDRESS in addresses
        ):
            raise ValueError("distinct bounded nonzero roles and token identities required")
        if (
            len(self.providers) != 2
            or len(set(self.providers)) != 2
            or any(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p) is None for p in self.providers)
        ):
            raise ValueError("two distinct named providers required")
        if not 1 <= len(self.pools) <= 16 or len({pool_id_from_key(k) for k in self.pools}) != len(
            self.pools
        ):
            raise ValueError("bounded unique pool keys required")
        for key in self.pools:
            if (
                key.currency0 != ZERO_ADDRESS
                or key.currency1 not in self.tokens
                or key.hooks != ZERO_ADDRESS
                or type(key.fee) is not int
                or type(key.tick_spacing) is not int
                or key.fee > 1_000_000
            ):
                raise ValueError("native hookless static-fee research pool required")

    def state_requests(self, head: BlockSnapshot) -> list[tuple[str, list[object]]]:
        if head.ref.chain_id != self.chain_id:
            raise ValueError("foreign current identity head")
        pin = {"blockHash": head.ref.block_hash, "requireCanonical": True}
        calls: list[tuple[str, list[object]]] = [
            ("eth_getCode", [address, pin])
            for address in (self.pool_manager, self.state_view, *self.tokens)
        ]
        calls += [
            ("eth_call", [{"to": t, "data": selector("decimals()")}, pin]) for t in self.tokens
        ]
        calls += [("eth_call", [{"to": self.state_view, "data": selector("poolManager()")}, pin])]
        for key in self.pools:
            args = encode(["bytes32"], [bytes.fromhex(pool_id_from_key(key)[2:])]).hex()
            for method in ("getSlot0(bytes32)", "getLiquidity(bytes32)"):
                calls.append(
                    ("eth_call", [{"to": self.state_view, "data": selector(method) + args}, pin])
                )
        return calls

    @property
    def successful_request_count(self) -> int:
        return 6 + 2 * len(self.tokens) + 2 * len(self.pools)


def _abi(types: list[str], value: object) -> tuple[object, ...]:
    if not isinstance(value, str):
        raise ValueError("ABI result must be hex bytes")
    raw = bytes.fromhex(normalize_hex(value)[2:])
    values = cast(tuple[object, ...], decode(types, raw))
    if encode(types, list(values)) != raw:
        raise ValueError("ABI result must be complete and canonical")
    return values


def _opening(exchanges: Sequence[RpcExchange], plan: CurrentIdentityPlan) -> BlockSnapshot:
    if len(exchanges) != plan.successful_request_count:
        raise ValueError("complete bounded current identity capture required")
    _, response = decode_exchange(exchanges[1])
    if "error" in response:
        raise ValueError("finalized header refused")
    return decode_header(response["result"], chain_id=plan.chain_id)


def replay_current_identity(
    plan: CurrentIdentityPlan,
    captures: tuple[tuple[str, Sequence[RpcExchange]], tuple[str, Sequence[RpcExchange]]],
    *,
    captured_at_seconds: int,
) -> dict[str, object]:
    """Derive identity observations; absence of proxy indicators never admits a token."""
    if type(captured_at_seconds) is not int or captured_at_seconds < 0:
        raise ValueError("integer capture time required")
    if tuple(c[0] for c in captures) != plan.providers:
        raise ValueError("current identity sources differ from plan")
    heads = tuple(_opening(c[1], plan) for c in captures)
    if heads[0] != heads[1]:
        raise ValueError("current finalized headers disagree")
    head = heads[0]
    if (
        head.ref.timestamp is None
        or not 0 <= captured_at_seconds - head.ref.timestamp <= plan.max_head_age_seconds
    ):
        raise ValueError("current research head future or stale")
    expected = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", ["finalized", False]),
        *plan.state_requests(head),
        ("eth_getBlockByNumber", [hex(head.ref.number), False]),
    ]
    results = []
    for _, exchanges in captures:
        values = successful_results(exchanges, expected)
        for exchange, (_, params) in zip(exchanges, expected, strict=True):
            actual, _ = decode_exchange(exchange)
            if canonical_json(actual) != canonical_json(params) or exchange.attempt != 1:
                raise ValueError("exact canonical requests without hidden retries required")
        if (
            values[0] != hex(plan.chain_id)
            or decode_header(values[-1], chain_id=plan.chain_id) != head
        ):
            raise ValueError("current identity chain or closing header differs")
        results.append(values)
    if canonical_json(results[0][2:-1]) != canonical_json(results[1][2:-1]):
        raise ValueError("current identity runtime or state disagreement")
    values = results[0]
    runtimes = []
    for address, value in zip(
        (plan.pool_manager, plan.state_view, *plan.tokens),
        values[2 : 4 + len(plan.tokens)],
        strict=True,
    ):
        if not isinstance(value, str):
            raise ValueError("runtime must be hex bytes")
        raw = bytes.fromhex(normalize_hex(value)[2:])
        if not 1 <= len(raw) <= 24576:
            raise ValueError("nonempty bounded deployed runtime required")
        runtimes.append(
            {
                "address": address,
                "runtime_keccak": "0x" + keccak(raw).hex(),
                "runtime_bytes": len(raw),
                "static_proxy_indicators": asdict(inspect_proxy_indicators(raw)),
            }
        )
    index = 4 + len(plan.tokens)
    decimals = [cast(int, _abi(["uint8"], v)[0]) for v in values[index : index + len(plan.tokens)]]
    if any(d > 77 for d in decimals):
        raise ValueError("token decimals outside accounting bounds")
    index += len(plan.tokens)
    if decode_address_word(values[index]) != plan.pool_manager:
        raise ValueError("StateView PoolManager binding differs")
    index += 1
    pools = []
    for key in plan.pools:
        sqrt, tick, protocol, fee = _abi(["uint160", "int24", "uint24", "uint24"], values[index])
        state = Slot0State(cast(int, sqrt), cast(int, tick), cast(int, protocol), cast(int, fee))
        liquidity = cast(int, _abi(["uint128"], values[index + 1])[0])
        if state.lp_fee != key.fee:
            raise ValueError("observed static LP fee differs from declared key")
        pools.append(
            {
                "pool_id": pool_id_from_key(key),
                "key": asdict(key),
                "slot0": asdict(state),
                "liquidity": liquidity,
            }
        )
        index += 2
    return {
        "schema": "oasis.current-pool-identity.v1",
        "header": asdict(head),
        "providers": list(plan.providers),
        "captured_at_seconds": captured_at_seconds,
        "runtimes": runtimes,
        "token_decimals": dict(zip(plan.tokens, decimals, strict=True)),
        "pools": pools,
        "runtime_and_state_quorum": True,
        "token_authority_verified": False,
        "deployed_source_reproduced": False,
        "target_admitted": False,
        "net_pnl": None,
        "oos_accepted": False,
        "canary_accepted": False,
        "execution_eligible": False,
    }
