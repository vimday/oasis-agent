"""Pinned Chainlink-style USD feed observations and conservative cost conversion.

Feed identity is a supplied research policy, not an independent certification.
Never assume a stablecoin equals USD. Native/quote conversion requires both feeds.
"""

from dataclasses import asdict, dataclass
from typing import cast

from eth_hash.auto import keccak

from ..evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
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
from ..rpc.transport import RpcExchange
from .owner_payments import _abi
from .position_settlement import _mapping, _text
from .transaction_costs import TransactionCostCapture


def selector(signature: str) -> str:
    return "0x" + keccak(signature.encode())[:4].hex()


@dataclass(frozen=True, slots=True)
class UsdFeedPolicy:
    chain_id: int
    address: str
    base_token: str
    description: str
    runtime_keccak: str
    max_age_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        object.__setattr__(self, "base_token", normalize_address(self.base_token))
        object.__setattr__(self, "runtime_keccak", normalize_hash32(self.runtime_keccak))
        if (
            type(self.chain_id) is not int
            or self.chain_id <= 0
            or type(self.max_age_seconds) is not int
            or self.max_age_seconds <= 0
        ):
            raise ValueError("positive feed chain and freshness policy required")
        if not self.description.endswith(" / USD") or not self.description.removesuffix(" / USD"):
            raise ValueError("explicit USD feed description required")
        if self.address == ZERO_ADDRESS:
            raise ValueError("feed address must be a contract")


@dataclass(frozen=True, slots=True)
class UsdPriceObservation:
    answer: int
    decimals: int
    round_id: int
    started_at: int
    updated_at: int
    answered_in_round: int

    def __post_init__(self) -> None:
        for name in (
            "answer",
            "decimals",
            "round_id",
            "started_at",
            "updated_at",
            "answered_in_round",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.answer <= 0 or not 0 <= self.decimals <= 255:
            raise ValueError("positive answer and uint8 decimals required")
        if self.round_id <= 0 or self.answered_in_round < self.round_id:
            raise ValueError("complete oracle round identity required")
        if not 0 < self.started_at <= self.updated_at:
            raise ValueError("valid oracle round timing required")


@dataclass(frozen=True, slots=True)
class UsdPriceCapture:
    header: BlockSnapshot
    policy: UsdFeedPolicy
    source: str
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if not self.source.strip():
            raise ValueError("price provider required")
        self.reconstruct()

    def reconstruct(self) -> tuple[int, int]:
        observation = self.reconstruct_observation()
        return observation.answer, observation.decimals

    def reconstruct_observation(self) -> UsdPriceObservation:
        p = self.policy
        b = self.header.ref
        if p.chain_id != b.chain_id:
            raise ValueError("feed chain mismatch")
        pin = {"blockHash": b.block_hash, "requireCanonical": True}
        specs: list[tuple[str, list[object]]] = [
            ("eth_chainId", []),
            ("eth_getBlockByNumber", [hex(b.number), False]),
            ("eth_getCode", [p.address, pin]),
        ]
        specs += [
            ("eth_call", [{"to": p.address, "data": selector(s)}, pin])
            for s in ("decimals()", "description()", "latestRoundData()")
        ]
        specs += [("eth_getBlockByNumber", [hex(b.number), False])]
        if (
            len(self.exchanges) != len(specs)
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 1_000_000
        ):
            raise ValueError("incomplete or oversized price evidence")
        results = []
        ids = set()
        for x, (m, params) in zip(self.exchanges, specs, strict=True):
            actual, response = decode_exchange(x)
            if (
                x.method != m
                or canonical_json(actual) != canonical_json(params)
                or "error" in response
                or x.request_id in ids
            ):
                raise ValueError("price request/response mismatch")
            ids.add(x.request_id)
            results.append(response["result"])
        if rpc_quantity(results[0]) != p.chain_id:
            raise ValueError("price provider chain mismatch")
        for i in (1, 6):
            h = _mapping(results[i])
            actual_header = BlockSnapshot(
                BlockRef(
                    p.chain_id,
                    rpc_quantity(h.get("number")),
                    _text(h.get("hash")),
                    rpc_quantity(h.get("timestamp")),
                ),
                _text(h.get("parentHash")),
            )
            if actual_header != self.header:
                raise ValueError("price block changed")
        code = _text(results[2])
        if (
            not code.startswith("0x")
            or not bytes.fromhex(code[2:])
            or "0x" + keccak(bytes.fromhex(code[2:])).hex() != p.runtime_keccak
        ):
            raise ValueError("feed runtime mismatch")
        decimals = cast(int, _abi(["uint8"], _text(results[3]))[0])
        if _abi(["string"], _text(results[4]))[0] != p.description:
            raise ValueError("feed description mismatch")
        round_id, answer, started, updated, answered = _abi(
            ["uint80", "int256", "uint256", "uint256", "uint80"], _text(results[5])
        )
        r, a, s, u, ar = map(
            int,
            cast(tuple[int, int, int, int, int], (round_id, answer, started, updated, answered)),
        )
        if (
            r == 0
            or a <= 0
            or not 0 < s <= u
            or ar < r
            or b.timestamp is None
            or not 0 <= b.timestamp - u <= p.max_age_seconds
        ):
            raise ValueError("invalid, stale, future or incomplete oracle round")
        return UsdPriceObservation(a, decimals, r, s, u, ar)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.price-feed.v1",
                "block": asdict(self.header.ref),
                "policy": asdict(self.policy),
                "answer_decimals": list(self.reconstruct()),
                "source": self.source,
            }
        )


def value_native_cost(
    cost: TransactionCostCapture,
    native_usd: UsdPriceCapture,
    quote_usd: UsdPriceCapture,
    *,
    quote_token: str,
    quote_decimals: int,
) -> int:
    quote_token = normalize_address(quote_token)
    if type(quote_decimals) is not int or not 0 <= quote_decimals <= 255:
        raise ValueError("quote decimals must fit uint8")
    if native_usd.header != cost.header or quote_usd.header != cost.header:
        raise ValueError("fee and prices must share the exact state snapshot")
    if native_usd.policy.base_token != ZERO_ADDRESS or quote_usd.policy.base_token != quote_token:
        raise ValueError("feed denomination mismatch")
    native, nd = native_usd.reconstruct()
    quote, qd = quote_usd.reconstruct()
    amount = cost.reconstruct().total_native
    numerator: int = amount * native * 10 ** (qd + quote_decimals)
    denominator: int = 10 ** (18 + nd) * quote
    return (numerator + denominator - 1) // denominator


@dataclass(frozen=True, slots=True)
class CostValuation:
    chain_id: int
    transaction_hash: str
    total_native: int
    native_answer: int
    native_decimals: int
    quote_token: str
    quote_decimals: int
    quote_answer: int
    quote_feed_decimals: int
    value_quote_units: int

    @property
    def usd_nominal(self) -> int:
        """Indicative USD total: quote units scaled by the observed quote/USD feed.

        This is not a peg assumption; it uses the pinned quote feed answer. The
        figure is a valuation reference, never settlement evidence.
        """
        num: int = self.value_quote_units * self.quote_answer
        den: int = 10**self.quote_feed_decimals
        return (num + den - 1) // den


def value_cost_in_quote(
    cost: TransactionCostCapture,
    native_usd: UsdPriceCapture,
    quote_usd: UsdPriceCapture,
    *,
    quote_token: str,
    quote_decimals: int,
) -> CostValuation:
    b = cost.header.ref
    native, nd = native_usd.reconstruct()
    quote, qd = quote_usd.reconstruct()
    total = cost.reconstruct().total_native
    value = value_native_cost(
        cost, native_usd, quote_usd, quote_token=quote_token, quote_decimals=quote_decimals
    )
    return CostValuation(
        b.chain_id,
        cost.transaction_hash,
        total,
        native,
        nd,
        normalize_address(quote_token),
        quote_decimals,
        quote,
        qd,
        value,
    )


def persist_price_evidence(store: EvidenceStore, capture: UsdPriceCapture) -> EvidenceRef:
    p = capture.policy
    payload = canonical_json(
        {
            "schema": "oasis.price-feed.v1",
            "block": asdict(capture.header.ref),
            "policy": {
                "chain_id": p.chain_id,
                "address": p.address,
                "base_token": p.base_token,
                "description": p.description,
                "runtime_keccak": p.runtime_keccak,
                "max_age_seconds": p.max_age_seconds,
            },
            "answer_decimals": capture.reconstruct(),
            "source": capture.source,
        }
    )

    def envelope(response: bool) -> dict[str, object]:
        return {
            "encoding": "hex_exact_bytes",
            "exchanges": [
                {
                    "method": x.method,
                    "request_id": x.request_id,
                    "attempt": x.attempt,
                    "payload": (x.response_payload if response else x.request_payload).hex(),
                }
                for x in capture.exchanges
            ],
        }

    return store.put(
        "price_feed",
        payload,
        capture.header.ref,
        capture.source,
        request=envelope(False),
        raw_response=envelope(True),
        interpretation={"scope": "single_usd_feed_observation", "execution_eligible": False},
    )


def replay_price_evidence(
    store: EvidenceStore, evidence: EvidenceRef, *, header: BlockSnapshot, policy: UsdFeedPolicy
) -> UsdPriceCapture:
    if evidence.kind != "price_feed" or evidence.block != header.ref:
        raise ValueError("price evidence scope mismatch")
    record = store.get_record(evidence)
    if record.request_payload is None:
        raise ValueError("price evidence missing wire requests")
    capture = UsdPriceCapture(
        header,
        policy,
        evidence.source,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload:
        raise ValueError("price interpretation differs from raw reconstruction")
    return capture


def _price_wire_envelope(capture: UsdPriceCapture, *, response: bool) -> dict[str, object]:
    return {
        "encoding": "hex_exact_bytes",
        "exchanges": [
            {
                "method": item.method,
                "request_id": item.request_id,
                "attempt": item.attempt,
                "payload": (item.response_payload if response else item.request_payload).hex(),
            }
            for item in capture.exchanges
        ],
    }


def _price_observation_payload(capture: UsdPriceCapture) -> bytes:
    return canonical_json(
        {
            "schema": "oasis.price-feed-observation.v2",
            "block": asdict(capture.header.ref),
            "policy": asdict(capture.policy),
            "observation": asdict(capture.reconstruct_observation()),
            "source": capture.source,
        }
    )


def persist_price_observation_evidence(
    store: EvidenceStore, capture: UsdPriceCapture
) -> EvidenceRef:
    return store.put(
        "price_feed_observation",
        _price_observation_payload(capture),
        capture.header.ref,
        capture.source,
        request=_price_wire_envelope(capture, response=False),
        raw_response=_price_wire_envelope(capture, response=True),
        interpretation={
            "schema": "oasis.price-feed-observation.v2",
            "validation": "raw_rpc_reconstruction",
            "execution_eligible": False,
        },
        schema_version=2,
    )


def replay_price_observation_evidence(
    store: EvidenceStore,
    evidence: EvidenceRef,
    *,
    header: BlockSnapshot,
    policy: UsdFeedPolicy,
) -> UsdPriceCapture:
    if (
        evidence.kind != "price_feed_observation"
        or evidence.schema_version != 2
        or evidence.block != header.ref
    ):
        raise ValueError("price observation evidence scope mismatch")
    record = store.get_record(evidence)
    if record.request_payload is None:
        raise ValueError("price observation evidence missing wire requests")
    capture = UsdPriceCapture(
        header,
        policy,
        evidence.source,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if _price_observation_payload(capture) != record.payload:
        raise ValueError("price observation interpretation differs from raw reconstruction")
    return capture


def persist_cost_valuation(
    store: EvidenceStore, valuation: CostValuation, *, block: BlockRef, source: str
) -> EvidenceRef:
    payload = canonical_json(
        {
            "schema": "oasis.cost-valuation.v1",
            "valuation": asdict(valuation),
            "source": source,
            "execution_eligible": False,
            "peg_assumption": False,
        }
    )
    return store.put(
        "cost_valuation",
        payload,
        block,
        source,
        interpretation={
            "scope": "transaction_native_cost_in_quote_units",
            "execution_eligible": False,
        },
    )
