"""Exact-wire quote/spot acquisition, without capital or signing capability."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from eth_abi.abi import decode, encode

from ..evidence.exact_capture import (
    RecordedSession,
    decode_address_word,
    decode_header,
    require_code_hash,
    successful_results,
)
from ..evidence.rpc_validation import decode_exchange
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import canonical_json, normalize_address, normalize_hash32
from ..models.v4 import PoolKey
from ..rpc.client import RpcClient
from ..rpc.transport import RpcExchange
from ..shadow.pool_state_capture import (
    GET_SLOT0_SELECTOR,
    POOL_MANAGER_SELECTOR,
    Slot0WindowCapture,
    Slot0WindowQuorum,
    persist_slot0_window_evidence,
)
from ..uniswap_v4.pool_id import pool_id_from_key
from ..uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    QuotePoolKey,
    SinglePoolQuoteRequest,
    decode_single_pool_quote,
    encode_single_pool_quote,
)
from ..uniswap_v4.state_rpc import Slot0State
from .quote_grid_replay import QuoteGridRequest, QuoteRuntimePin, replay_quote_grid

ROLES = ("pool_manager", "quoter", "universal_router")


@dataclass(frozen=True, slots=True)
class QuoteCapturePlan:
    chain_id: int
    block_number: int
    pool: QuotePoolKey
    runtimes: tuple[QuoteRuntimePin, ...]
    state_view: str
    state_view_runtime_keccak: str
    providers: tuple[str, str]
    probes: tuple[SinglePoolQuoteRequest, ...]

    def __post_init__(self) -> None:
        if (
            type(self.chain_id) is not int
            or self.chain_id <= 0
            or type(self.block_number) is not int
            or self.block_number < 0
        ):
            raise ValueError("explicit chain and block number required")
        if tuple(p.role for p in self.runtimes) != ROLES:
            raise ValueError("ordered pool/quoter/router runtime pins required")
        if (
            len(self.providers) != 2
            or len(set(self.providers)) != 2
            or any(not p for p in self.providers)
        ):
            raise ValueError("two distinct declared quote sources required")
        object.__setattr__(self, "state_view", normalize_address(self.state_view))
        object.__setattr__(
            self, "state_view_runtime_keccak", normalize_hash32(self.state_view_runtime_keccak)
        )
        if int(self.state_view, 16) == 0 or not 12 <= len(self.probes) <= 64:
            raise ValueError("nonzero state view and bounded quote grid required")
        groups: dict[tuple[QuoteKind, QuoteDirection], set[int]] = {}
        for p in self.probes:
            group = groups.setdefault((p.kind, p.direction), set())
            if p.amount in group:
                raise ValueError("duplicate quote size")
            group.add(p.amount)
        if set(groups) != {(k, d) for k in QuoteKind for d in QuoteDirection} or any(
            len(s) < 3 for s in groups.values()
        ):
            raise ValueError("quote grid requires both kinds/directions and three sizes")

    @property
    def pool_id(self) -> str:
        return pool_id_from_key(
            PoolKey(
                self.pool.currency0,
                self.pool.currency1,
                self.pool.fee,
                self.pool.tick_spacing,
                self.pool.hooks,
            )
        )

    @property
    def successful_request_count(self) -> int:
        return 10 + len(self.probes)

    def expected(self, header: BlockSnapshot) -> list[tuple[str, list[object]]]:
        pin = {"blockHash": header.ref.block_hash, "requireCanonical": True}
        codes = [
            (self.state_view, self.state_view_runtime_keccak),
            *((p.address, p.runtime_keccak) for p in self.runtimes),
        ]
        calls = [("eth_chainId", []), ("eth_getBlockByNumber", [hex(self.block_number), False])]
        calls += [("eth_getCode", [address, pin]) for address, _ in codes]
        calls += [
            ("eth_call", [{"to": target, "data": POOL_MANAGER_SELECTOR}, pin])
            for target in (self.state_view, self.runtimes[1].address)
        ]
        data = GET_SLOT0_SELECTOR + encode(["bytes32"], [bytes.fromhex(self.pool_id[2:])]).hex()
        calls += [("eth_call", [{"to": self.state_view, "data": data}, pin])]
        calls += [
            (
                "eth_call",
                [
                    {
                        "to": self.runtimes[1].address,
                        "data": encode_single_pool_quote(self.pool, p),
                    },
                    pin,
                ],
            )
            for p in self.probes
        ]
        return [*calls, ("eth_getBlockByNumber", [hex(self.block_number), False])]


@dataclass(frozen=True, slots=True)
class QuoteSourceCapture:
    plan: QuoteCapturePlan
    source: str
    header: BlockSnapshot
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if (
            self.source not in self.plan.providers
            or self.header.ref.chain_id != self.plan.chain_id
            or self.header.ref.number != self.plan.block_number
        ):
            raise ValueError("quote capture source or header differs from plan")
        expected = self.plan.expected(self.header)
        results = successful_results(self.exchanges, expected)
        for exchange, (_, params) in zip(self.exchanges, expected, strict=True):
            actual, _ = decode_exchange(exchange)
            if canonical_json(actual) != canonical_json(params):
                raise ValueError("quote capture params must be exactly canonical")
        if results[0] != hex(self.plan.chain_id) or any(
            decode_header(results[i], chain_id=self.plan.chain_id) != self.header for i in (1, -1)
        ):
            raise ValueError("quote capture chain/header changed")
        hashes = (
            self.plan.state_view_runtime_keccak,
            *(p.runtime_keccak for p in self.plan.runtimes),
        )
        for code, digest in zip(results[2:6], hashes, strict=True):
            require_code_hash(code, digest)
        if any(decode_address_word(results[i]) != self.plan.runtimes[0].address for i in (6, 7)):
            raise ValueError("quote capture PoolManager getters differ")
        self.slot0_capture()
        for value in results[9:-1]:
            if not isinstance(value, str) or not decode_single_pool_quote(value).quoted_amount:
                raise ValueError("quote capture needs successful positive ABI quotes")

    def slot0_capture(self) -> Slot0WindowCapture:
        _, response = decode_exchange(self.exchanges[8])
        raw = response["result"]
        if not isinstance(raw, str) or len(raw) != 258:
            raise ValueError("quote capture slot0 ABI length differs")
        sqrt, tick, protocol, lp = decode(
            ["uint160", "int24", "uint24", "uint24"], bytes.fromhex(raw[2:]), strict=True
        )
        state = Slot0State(int(sqrt), int(tick), int(protocol), int(lp))
        return Slot0WindowCapture(
            self.source,
            (self.header,),
            self.plan.state_view,
            self.plan.runtimes[0].address,
            self.plan.pool_id,
            self.plan.state_view_runtime_keccak,
            self.plan.runtimes[0].runtime_keccak,
            (state,),
            tuple(self.exchanges[i] for i in (0, 1, 2, 3, 6, 8, len(self.exchanges) - 1)),
        )


def collect_quote_source(session: RecordedSession, plan: QuoteCapturePlan) -> QuoteSourceCapture:
    transport, recorder, source = session
    if recorder.exchanges or source not in plan.providers:
        raise ValueError("declared fresh quote session required")
    client = RpcClient(transport, expected_chain_id=plan.chain_id)
    opening = client.get_block_snapshot(plan.block_number)
    # The first chain/header reads establish the only hash all state calls may use.
    for method, params in plan.expected(opening)[2:]:
        transport.request(method, params)
    return QuoteSourceCapture(plan, source, opening, recorder.exchanges)


def retain_quote_grid(
    store: EvidenceStore, captures: tuple[QuoteSourceCapture, QuoteSourceCapture]
) -> QuoteGridRequest:
    left, right = captures
    if (
        left.plan != right.plan
        or (left.source, right.source) != left.plan.providers
        or left.header != right.header
    ):
        raise ValueError("quote capture quorum identity/header differs")
    plan = left.plan
    state_quorum = Slot0WindowQuorum(left.slot0_capture(), right.slot0_capture())
    # Compare decoded results; distinct RPC IDs and raw provider provenance are preserved.
    if tuple(decode_exchange(e)[1]["result"] for e in left.exchanges[2:-1]) != tuple(
        decode_exchange(e)[1]["result"] for e in right.exchanges[2:-1]
    ):
        raise ValueError("quote providers disagree on raw results")
    states = persist_slot0_window_evidence(store, state_quorum)
    refs = []
    timestamp = datetime.now(UTC).isoformat()
    for capture in captures:
        for i in (3, 4, 5, 7, *range(9, len(capture.exchanges) - 1)):
            exchange = capture.exchanges[i]
            _, response = decode_exchange(exchange)
            if i <= 5:
                pin = plan.runtimes[i - 3]
                fact = {
                    "role": pin.role,
                    "address": pin.address,
                    "runtime_keccak": pin.runtime_keccak,
                }
                interpretation = {
                    "schema": "oasis.route-probe.v1",
                    "runtime_keccak": pin.runtime_keccak,
                }
            elif i == 7:
                fact = {
                    "quoter": plan.runtimes[1].address,
                    "pool_manager": plan.runtimes[0].address,
                }
                interpretation = {"schema": "oasis.route-probe.v1", "binding_matches": True}
            else:
                p = plan.probes[i - 9]
                value = response["result"]
                if not isinstance(value, str):
                    raise ValueError("quote result must be hex")
                quote = decode_single_pool_quote(value)
                fact = {
                    "kind": p.kind.value,
                    "direction": p.direction.value,
                    "amount": p.amount,
                    "hook_data_sha256": sha256(bytes.fromhex(p.hook_data[2:])).hexdigest(),
                    "status": "ok",
                    "quoted_amount": quote.quoted_amount,
                    "gas_estimate": quote.gas_estimate,
                }
                interpretation = {"schema": "oasis.route-probe.v1", **fact}
            req = {
                "method": exchange.method,
                "request_id": exchange.request_id,
                "attempt": exchange.attempt,
                "payload": exchange.request_payload.hex(),
            }
            res = {**req, "payload": exchange.response_payload.hex()}
            refs.append(
                store.put(
                    "v4_route_probe",
                    canonical_json(fact),
                    capture.header.ref,
                    capture.source,
                    request=req,
                    raw_response=res,
                    interpretation=interpretation,
                    retrieved_at=timestamp,
                    schema_version=1,
                )
            )
    request = QuoteGridRequest(
        left.header.ref, plan.pool, plan.runtimes, plan.probes, plan.providers, tuple(refs), states
    )
    replay_quote_grid(store, store, request)
    return request
