"""Two-provider retained account NAV, including v4 fees and both USD feeds.

Feed pins are the Base declarations recorded in OBSERVED_LP_INVENTORY_PRICE_20261002.
Every capture verifies them again; a declaration alone supplies no observed price.
USD values floor each asset leg. Raw units and feed answers never pass through float.
"""

import json
from dataclasses import asdict, dataclass

from eth_abi.abi import decode, encode

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
from ..farming.positions import position_id
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    evidence_ref_from_json,
    sha256_hex,
)
from ..rpc.transport import RpcExchange
from ..uniswap_v4.liquidity_math import MAX_TICK, sqrt_price_at_tick
from .config import LiveConfig
from .positions import _call, collect_position_outcome, replay_position_outcome
from .state import Position
from .v4lp import position_amounts
from .wallet import WalletBalance, WalletScope, capture_wallet, replay_wallet


class NavRefusal(ValueError):
    """Fixed, non-sensitive valuation refusal; safe to report to the operator."""


USDC_USD_FEED = "0x7e860098f58bbfc8648a4311b374b1d669a2bc6b"
ETH_USD_FEED = "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70"
FEED_RUNTIME = "0xbd6f524cdc4268b6bd1bb6f77a8821faeea9c52ee9e0afa0b6d948ce82c966c2"


def _abi(types: list[str], raw: object) -> tuple[object, ...]:
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise NavRefusal("valuation ABI bytes missing")
    data = bytes.fromhex(raw[2:])
    values = tuple(decode(types, data))
    if encode(types, values) != data:
        raise NavRefusal("noncanonical valuation ABI")
    return values


def requests(
    c: LiveConfig, block: BlockRef, position: Position | None
) -> list[tuple[str, list[object]]]:
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    result: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getCode", [c.state_view, pin]),
        _call(c.state_view, "getSlot0(bytes32)", ["bytes32"], [bytes.fromhex(c.pool_id[2:])], pin),
    ]
    for feed in (c.eth_usd_feed, USDC_USD_FEED):
        result.append(("eth_getCode", [feed, pin]))
        result.extend(
            _call(feed, sig, [], [], pin)
            for sig in ("decimals()", "description()", "latestRoundData()")
        )
    if position is not None:
        core = position_id(c.position_manager, position.lower, position.upper, position.token_id)
        result.extend(
            [
                _call(
                    c.state_view,
                    "getPositionInfo(bytes32,bytes32)",
                    ["bytes32", "bytes32"],
                    [bytes.fromhex(c.pool_id[2:]), bytes.fromhex(core[2:])],
                    pin,
                ),
                _call(
                    c.state_view,
                    "getFeeGrowthInside(bytes32,int24,int24)",
                    ["bytes32", "int24", "int24"],
                    [bytes.fromhex(c.pool_id[2:]), position.lower, position.upper],
                    pin,
                ),
            ]
        )
    result.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return result


@dataclass(frozen=True, slots=True)
class AccountValue:
    wallet: WalletBalance
    sqrt_price: int
    tick: int
    eth_answer: int
    usdc_answer: int
    principal0: int
    principal1: int
    fee0: int
    fee1: int
    nav_micros: int

    @property
    def position_micros(self) -> int:
        return (self.principal0 + self.fee0) * self.eth_answer // 10**20 + (
            self.principal1 + self.fee1
        ) * self.usdc_answer // 10**8


@dataclass(frozen=True, slots=True)
class NavCapture:
    wallet: EvidenceRef
    position: Position | None
    position_evidence: EvidenceRef | None
    exchanges: tuple[RpcExchange, ...]

    def reconstruct(self, store: EvidenceStore, c: LiveConfig) -> AccountValue:
        proof = replay_wallet(store, self.wallet)
        wallet = proof.reconstruct()
        block = wallet.header.ref
        if proof.scope != WalletScope.from_config(c) or c.eth_usd_feed != ETH_USD_FEED:
            raise NavRefusal("valuation scope differs from pinned Base policy")
        if block.timestamp is None or (self.position is None) != (self.position_evidence is None):
            raise NavRefusal("position or timestamp evidence missing")
        if self.position is not None and self.position_evidence is not None:
            p = replay_position_outcome(store, self.position_evidence)
            if (
                p.block != block
                or self.position_evidence.source != self.wallet.source
                or p.scope.account != c.account
                or p.scope.pool_id != c.pool_id
                or p.scope.position_manager != c.position_manager
                or p.scope.state_view != c.state_view
                or p.scope.position_manager_hash != c.expected_pins["position_manager"]
                or p.scope.state_view_hash != c.expected_pins["state_view"]
                or (
                    p.scope.token_id,
                    p.scope.lower,
                    p.scope.upper,
                    p.scope.liquidity,
                    p.scope.burned,
                )
                != (
                    self.position.token_id,
                    self.position.lower,
                    self.position.upper,
                    self.position.liquidity,
                    False,
                )
            ):
                raise NavRefusal("NAV position proof differs from declared holding")
        if sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges) > 500_000:
            raise NavRefusal("NAV evidence byte budget exceeded")
        vals = successful_results(self.exchanges, requests(c, block, self.position))
        if (
            rpc_quantity(vals[0]) != c.chain_id
            or decode_header(vals[1], chain_id=c.chain_id) != wallet.header
            or decode_header(vals[-1], chain_id=c.chain_id) != wallet.header
        ):
            raise NavRefusal("NAV chain/header changed")
        require_code_hash(vals[2], c.expected_pins["state_view"])
        sqrt, tick, _, _ = _abi(["uint160", "int24", "uint24", "uint24"], vals[3])
        assert isinstance(sqrt, int) and isinstance(tick, int)
        if tick >= MAX_TICK or not sqrt_price_at_tick(tick) <= sqrt <= sqrt_price_at_tick(tick + 1):
            raise NavRefusal("NAV pool price/tick inconsistent")
        answers = []
        for i, description in ((4, "ETH / USD"), (8, "USDC / USD")):
            require_code_hash(vals[i], FEED_RUNTIME)
            if _abi(["uint8"], vals[i + 1]) != (8,) or _abi(["string"], vals[i + 2]) != (
                description,
            ):
                raise NavRefusal("NAV feed identity/decimals changed")
            round_id, answer, started, updated, answered = _abi(
                ["uint80", "int256", "uint256", "uint256", "uint80"], vals[i + 3]
            )
            assert all(isinstance(v, int) for v in (round_id, answer, started, updated, answered))
            if not (
                isinstance(answer, int)
                and isinstance(updated, int)
                and isinstance(started, int)
                and isinstance(round_id, int)
                and isinstance(answered, int)
            ):
                raise NavRefusal("invalid feed integers")
            if (
                round_id <= 0
                or answer <= 0
                or answered < round_id
                or not 0 < started <= updated
                or not 0 <= block.timestamp - updated <= c.oracle_max_age_seconds
            ):
                raise NavRefusal(f"NAV {description} feed incomplete, stale or future")
            answers.append(answer)
        principal0 = principal1 = fee0 = fee1 = 0
        if self.position is not None:
            liquidity, last0, last1 = _abi(["uint128", "uint256", "uint256"], vals[12])
            growth0, growth1 = _abi(["uint256", "uint256"], vals[13])
            if liquidity != self.position.liquidity:
                raise NavRefusal("manager/core liquidity disagreement")
            assert isinstance(liquidity, int) and isinstance(last0, int) and isinstance(last1, int)
            assert isinstance(growth0, int) and isinstance(growth1, int)
            fee0 = ((growth0 - last0) % 2**256) * liquidity // 2**128
            fee1 = ((growth1 - last1) % 2**256) * liquidity // 2**128
            if max(fee0, fee1) >= 2**127:
                raise NavRefusal("claimable fees exceed protocol signed balance bounds")
            principal0, principal1 = position_amounts(
                sqrt, self.position.lower, self.position.upper, liquidity
            )
        # Each wallet/position leg floors independently, never inflating NAV.
        nav = (
            wallet.native * answers[0] // 10**20
            + wallet.token * answers[1] // 10**8
            + (principal0 + fee0) * answers[0] // 10**20
            + (principal1 + fee1) * answers[1] // 10**8
        )
        return AccountValue(
            wallet, sqrt, tick, answers[0], answers[1], principal0, principal1, fee0, fee1, nav
        )

    def payload(self, store: EvidenceStore, c: LiveConfig) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.live-nav.v1",
                "config_hash": sha256_hex(canonical_json(asdict(c))),
                "wallet": asdict(self.wallet),
                "position": asdict(self.position) if self.position else None,
                "position_evidence": asdict(self.position_evidence)
                if self.position_evidence
                else None,
                "value": asdict(self.reconstruct(store, c)),
                "execution_eligible": False,
            }
        )


def replay_nav(store: EvidenceStore, ref: EvidenceRef, c: LiveConfig) -> NavCapture:
    record = store.get_record(ref)
    if ref.kind != "live_nav" or record.request_payload is None:
        raise NavRefusal("NAV raw proof missing")
    d = json.loads(record.payload)
    capture = NavCapture(
        evidence_ref_from_json(d["wallet"]),
        Position(**d["position"]) if d["position"] else None,
        evidence_ref_from_json(d["position_evidence"]) if d["position_evidence"] else None,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if (
        ref.block != capture.wallet.block
        or ref.source != capture.wallet.source
        or capture.payload(store, c) != record.payload
    ):
        raise NavRefusal("NAV interpretation contradicts raw bytes")
    return capture


def capture_nav(
    store: EvidenceStore,
    c: LiveConfig,
    block: BlockRef,
    position: Position | None,
    *,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[AccountValue, tuple[EvidenceRef, EvidenceRef]]:
    wallets = capture_wallet(store, c, block, sources=sources, sessions=sessions)
    positions = (
        None
        if position is None
        else collect_position_outcome(
            store,
            c,
            block,
            token_id=position.token_id,
            lower=position.lower,
            upper=position.upper,
            liquidity=position.liquidity,
            burned=False,
            sources=sources,
            sessions=sessions,
        )
    )
    pair = (sessions(sources[0]), sessions(sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != sources:
        raise NavRefusal("NAV source aliases changed")
    captures = []
    for i, session in enumerate(pair):
        for method, params in requests(c, block, position):
            session[0].request(method, params)
        captures.append(
            NavCapture(
                wallets[i], position, positions[i] if positions else None, session[1].exchanges
            )
        )
    if captures[0].reconstruct(store, c) != captures[1].reconstruct(store, c):
        raise NavRefusal("NAV providers disagree")
    # Compare complete observed rounds/core state too, not only rounded valuations.
    results = [successful_results(x.exchanges, requests(c, block, position)) for x in captures]
    if results[0] != results[1]:
        raise NavRefusal("NAV raw observations disagree")
    refs = tuple(
        store.put(
            "live_nav",
            p.payload(store, c),
            block,
            p.wallet.source,
            request=wire_envelope(p.exchanges, response=False),
            raw_response=wire_envelope(p.exchanges, response=True),
            interpretation={"execution_eligible": False},
        )
        for p in captures
    )
    for ref in refs:
        replay_nav(store, ref, c)
    return captures[0].reconstruct(store, c), (refs[0], refs[1])
