"""Replay historical quotes against raw same-block spot; never admit a target.

Sampled direct quotes bound only sampled quoted capacity, not settled exit depth.
The diagnostic thresholds are not an operator's approved impact policy.
"""

import re
from dataclasses import dataclass, field
from hashlib import sha256

from eth_hash.auto import keccak

from ..evidence.exact_capture import decode_address_word, require_code_hash
from ..evidence.rpc_validation import decode_exchange, strict_json_object
from ..evidence.store import EvidenceStore
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from ..models.v4 import PoolKey
from ..rpc.transport import RpcExchange
from ..shadow.pool_state_capture import Slot0WindowQuorum, replay_slot0_window_evidence
from ..uniswap_v4.pool_id import pool_id_from_key
from ..uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    QuotePoolKey,
    SinglePoolQuoteRequest,
    decode_single_pool_quote,
    encode_single_pool_quote,
)

Q192 = 1 << 192
BINDING = "0x" + keccak(b"poolManager()")[:4].hex()


@dataclass(frozen=True, slots=True)
class QuoteRuntimePin:
    role: str
    address: str
    runtime_keccak: str

    def __post_init__(self) -> None:
        if self.role not in {"pool_manager", "quoter", "universal_router"}:
            raise ValueError("unknown quote runtime role")
        object.__setattr__(self, "address", normalize_address(self.address))
        object.__setattr__(self, "runtime_keccak", normalize_hash32(self.runtime_keccak))
        if int(self.address, 16) == 0:
            raise ValueError("nonzero quote runtime address required")


@dataclass(frozen=True, slots=True)
class QuoteGridRequest:
    block: BlockRef
    pool: QuotePoolKey
    runtimes: tuple[QuoteRuntimePin, ...]
    probes: tuple[SinglePoolQuoteRequest, ...]
    providers: tuple[str, str]
    references: tuple[EvidenceRef, ...]
    states: tuple[EvidenceRef, EvidenceRef]

    def __post_init__(self) -> None:
        if len(set(self.providers)) != 2 or any(not p for p in self.providers):
            raise ValueError("two distinct quote providers required")
        if len(self.runtimes) != 3 or {p.role for p in self.runtimes} != {
            "pool_manager",
            "quoter",
            "universal_router",
        }:
            raise ValueError("all three quote runtime pins required")
        groups: dict[tuple[QuoteKind, QuoteDirection], set[int]] = {}
        for probe in self.probes:
            sizes = groups.setdefault((probe.kind, probe.direction), set())
            if probe.amount in sizes:
                raise ValueError("duplicate quote grid size")
            sizes.add(probe.amount)
        if set(groups) != {(k, d) for k in QuoteKind for d in QuoteDirection} or any(
            len(sizes) < 3 for sizes in groups.values()
        ):
            raise ValueError("both quote kinds and directions require at least three sizes")


@dataclass(frozen=True, slots=True)
class QuoteImpact:
    kind: QuoteKind
    direction: QuoteDirection
    amount_in: int
    amount_out: int
    gas_estimate: int
    shortfall_numerator: int
    shortfall_denominator: int
    shortfall_ppm_ceiling: int


@dataclass(frozen=True, slots=True)
class SampledCapacity:
    diagnostic_shortfall_ppm: int
    zero_for_one_input_units0: int
    one_for_zero_input_units1: int
    two_sided_spot_notional_floor_units1: int


@dataclass(frozen=True, slots=True)
class ReplayedQuoteGrid:
    block: BlockRef
    pool: QuotePoolKey
    sqrt_price_x96: int
    quotes: tuple[QuoteImpact, ...]
    sampled_capacity: tuple[SampledCapacity, ...]
    evidence: tuple[EvidenceRef, ...]
    scope: str = field(default="historical_direct_quote_shortfall_vs_same_block_spot", init=False)
    approved_impact_threshold: None = field(default=None, init=False)
    settled_exit_depth: None = field(default=None, init=False)
    target_admitted: bool = field(default=False, init=False)
    execution_eligible: bool = field(default=False, init=False)


def _wire(store: EvidenceStore, ref: EvidenceRef) -> tuple[RpcExchange, dict[str, object]]:
    if ref.kind != "v4_route_probe" or ref.schema_version != 1:
        raise ValueError("retained route-probe wire evidence required")
    record = store.get_record(ref)
    if record.request_payload is None or record.interpretation_payload is None:
        raise ValueError("retained quote request and interpretation required")
    req = strict_json_object(record.request_payload)
    res = strict_json_object(record.raw_response_payload)
    for env in (req, res):
        if set(env) != {"method", "request_id", "attempt", "payload"}:
            raise ValueError("retained single exchange envelope malformed")
        if (
            not isinstance(env["payload"], str)
            or re.fullmatch(r"(?:[0-9a-f]{2})*", env["payload"]) is None
        ):
            raise ValueError("retained single exchange bytes missing")
    if any(req[k] != res[k] for k in ("method", "request_id", "attempt")):
        raise ValueError("quote envelope metadata disagrees")
    exchange = RpcExchange(
        str(req["method"]),
        req["request_id"],  # type: ignore[arg-type]
        bytes.fromhex(str(req["payload"])),
        bytes.fromhex(str(res["payload"])),
        req["attempt"],  # type: ignore[arg-type]
    )
    _, response = decode_exchange(exchange)
    if "error" in response:
        raise ValueError("retained quote or identity call failed")
    return exchange, strict_json_object(record.payload)


def replay_quote_grid(
    quote_store: EvidenceStore, state_store: EvidenceStore, request: QuoteGridRequest
) -> ReplayedQuoteGrid:
    """Re-derive every quote and pin from retained bytes, never report booleans."""
    captures = tuple(replay_slot0_window_evidence(state_store, ref) for ref in request.states)
    quorum = Slot0WindowQuorum(*captures)
    left = quorum.left
    expected_pool_id = pool_id_from_key(
        PoolKey(
            request.pool.currency0,
            request.pool.currency1,
            request.pool.fee,
            request.pool.tick_spacing,
            request.pool.hooks,
        )
    )
    pins = {p.role: p for p in request.runtimes}
    if (
        tuple(c.source for c in captures) != request.providers
        or len(left.headers) != 1
        or left.headers[0].ref != request.block
        or left.pool_id != expected_pool_id
        or left.pool_manager != pins["pool_manager"].address
        or left.pool_manager_runtime_keccak != pins["pool_manager"].runtime_keccak
    ):
        raise ValueError("quote spot proof differs from declared block/pool/providers/runtime")
    sqrt = left.slot0[0].sqrt_price_x96
    if sqrt <= 0:
        raise ValueError("initialized positive pool spot required")
    count = 4 + len(request.probes)
    if len(request.references) != count * 2:
        raise ValueError("quote reference coverage incomplete or extra")
    observed = []
    for source in request.providers:
        refs = tuple(r for r in request.references if r.source == source)
        if len(refs) != count:
            raise ValueError("quote provider coverage incomplete")
        seen: set[int] = set()
        rows = []
        for index, ref in enumerate(refs):
            if ref.block != request.block:
                raise ValueError("quote reference uses another block")
            exchange, fact = _wire(quote_store, ref)
            params, response = decode_exchange(exchange)
            if exchange.request_id in seen:
                raise ValueError("duplicate quote RPC request ID")
            seen.add(exchange.request_id)
            pin = {"blockHash": request.block.block_hash, "requireCanonical": True}
            derived: dict[str, object]
            interpretation: dict[str, object]
            if index < 3:
                identity = request.runtimes[index]
                expected = [identity.address, pin]
                method = "eth_getCode"
                require_code_hash(response["result"], identity.runtime_keccak)
                derived = {
                    "role": identity.role,
                    "address": identity.address,
                    "runtime_keccak": identity.runtime_keccak,
                }
                interpretation = {
                    "schema": "oasis.route-probe.v1",
                    "runtime_keccak": identity.runtime_keccak,
                }
            elif index == 3:
                expected = [{"to": pins["quoter"].address, "data": BINDING}, pin]
                method = "eth_call"
                binding = decode_address_word(response["result"])
                if binding != pins["pool_manager"].address:
                    raise ValueError("quoter manager binding differs")
                derived = {"quoter": pins["quoter"].address, "pool_manager": binding}
                interpretation = {"schema": "oasis.route-probe.v1", "binding_matches": True}
            else:
                probe = request.probes[index - 4]
                expected = [
                    {
                        "to": pins["quoter"].address,
                        "data": encode_single_pool_quote(request.pool, probe),
                    },
                    pin,
                ]
                method = "eth_call"
                value = response["result"]
                if not isinstance(value, str):
                    raise ValueError("quote result must be hex")
                quote = decode_single_pool_quote(value)
                if not quote.quoted_amount:
                    raise ValueError("quote output/input must be positive")
                derived = {
                    "kind": probe.kind.value,
                    "direction": probe.direction.value,
                    "amount": probe.amount,
                    "hook_data_sha256": sha256(bytes.fromhex(probe.hook_data[2:])).hexdigest(),
                }
                derived.update(
                    status="ok", quoted_amount=quote.quoted_amount, gas_estimate=quote.gas_estimate
                )
                interpretation = {"schema": "oasis.route-probe.v1", **derived}
                amount_in, amount_out = (
                    (probe.amount, quote.quoted_amount)
                    if probe.kind is QuoteKind.EXACT_INPUT
                    else (quote.quoted_amount, probe.amount)
                )
                numerator, denominator = (
                    (amount_in * sqrt**2 - amount_out * Q192, amount_in * sqrt**2)
                    if probe.direction is QuoteDirection.ZERO_FOR_ONE
                    else (amount_in * Q192 - amount_out * sqrt**2, amount_in * Q192)
                )
                rows.append(
                    QuoteImpact(
                        probe.kind,
                        probe.direction,
                        amount_in,
                        amount_out,
                        quote.gas_estimate,
                        numerator,
                        denominator,
                        -(-numerator * 1_000_000 // denominator),
                    )
                )
            if exchange.method != method or canonical_json(params) != canonical_json(expected):
                raise ValueError("quote wire call is not the expected hash-pinned read")
            if canonical_json(fact) != canonical_json(derived):
                raise ValueError("quote decoded fact contradicts raw result")
            record = quote_store.get_record(ref)
            if record.interpretation_payload != canonical_json(interpretation):
                raise ValueError("quote interpretation contradicts raw evidence")
        observed.append(tuple(rows))
    if observed[0] != observed[1]:
        raise ValueError("raw quote providers disagree")
    capacities = []
    for threshold in (100, 1000, 10000):
        sizes = {
            direction: max(
                (
                    q.amount_in
                    for q in observed[0]
                    if q.kind is QuoteKind.EXACT_INPUT
                    and q.direction is direction
                    and q.shortfall_numerator * 1_000_000 <= threshold * q.shortfall_denominator
                ),
                default=0,
            )
            for direction in QuoteDirection
        }
        a, b = sizes[QuoteDirection.ZERO_FOR_ONE], sizes[QuoteDirection.ONE_FOR_ZERO]
        capacities.append(SampledCapacity(threshold, a, b, min(a * sqrt**2 // Q192, b)))
    return ReplayedQuoteGrid(
        request.block,
        request.pool,
        sqrt,
        observed[0],
        tuple(capacities),
        (*request.references, *request.states),
    )
