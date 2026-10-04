"""Replay complete outer-router transaction coverage at one historical block.

This observes actual receipts, including reverts. It never equates Swap deltas
with owner cash, USD depth, settled exit capacity, or strategy return.
"""

from dataclasses import asdict, dataclass

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    require_code_hash,
    successful_results,
    wire_envelope,
)
from ..evidence.rpc_validation import (
    decode_exchange,
    restore_exchanges,
    rpc_quantity,
    strict_integer,
    strict_list,
    strict_object,
    strict_string,
)
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import (
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    normalize_hex,
)
from ..rpc.transport import RpcExchange
from ..uniswap_v4.events import SWAP_TOPIC, decode_swap
from ..uniswap_v4.fees import validate_lp_fee
from ..uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MAX_TICK, MIN_SQRT_PRICE, MIN_TICK


@dataclass(frozen=True, slots=True)
class RouterActivityPlan:
    header: BlockSnapshot
    pool_manager: str
    pool_manager_runtime_keccak: str
    router: str
    router_runtime_keccak: str
    pool_id: str
    providers: tuple[str, str]
    max_router_transactions: int

    def __post_init__(self) -> None:
        for field in ("pool_manager", "router"):
            object.__setattr__(self, field, normalize_address(getattr(self, field)))
        for field in ("pool_manager_runtime_keccak", "router_runtime_keccak", "pool_id"):
            object.__setattr__(self, field, normalize_hash32(getattr(self, field)))
        if (
            self.pool_manager == self.router
            or not int(self.pool_manager, 16)
            or not int(self.router, 16)
            or len(self.providers) != 2
            or len(set(self.providers)) != 2
            or any(not s for s in self.providers)
            or type(self.max_router_transactions) is not int
            or not 1 <= self.max_router_transactions <= 64
        ):
            raise ValueError("bounded distinct router activity scope required")

    def opening(self) -> list[tuple[str, list[object]]]:
        pin = {"blockHash": self.header.ref.block_hash, "requireCanonical": True}
        return [
            ("eth_chainId", []),
            ("eth_getBlockByNumber", [hex(self.header.ref.number), True]),
            ("eth_getCode", [self.pool_manager, pin]),
            ("eth_getCode", [self.router, pin]),
        ]


def _hash(value: object) -> str:
    return normalize_hash32(strict_string(value, "hash"))


def _address(value: object) -> str:
    return normalize_address(strict_string(value, "address"))


def router_transactions(plan: RouterActivityPlan, raw: object) -> tuple[dict[str, object], ...]:
    block = strict_object(raw, "full block")
    if decode_header(block, chain_id=plan.header.ref.chain_id) != plan.header:
        raise ValueError("full block differs from frozen header")
    txs = strict_list(block.get("transactions"), "full transactions")
    if len(txs) > 4096:
        raise ValueError("full block exceeds structural bound")
    selected = []
    seen = set()
    for index, item in enumerate(txs):
        tx = strict_object(item, "full transaction")
        digest = _hash(tx.get("hash"))
        if (
            digest in seen
            or _hash(tx.get("blockHash")) != plan.header.ref.block_hash
            or rpc_quantity(tx.get("blockNumber")) != plan.header.ref.number
            or rpc_quantity(tx.get("transactionIndex")) != index
        ):
            raise ValueError("full transaction coverage or block binding differs")
        seen.add(digest)
        target = None if tx.get("to") is None else _address(tx.get("to"))
        if target != plan.router:
            continue
        selected.append(
            {
                "hash": digest,
                "index": index,
                "from": _address(tx.get("from")),
                "to": target,
                "input": normalize_hex(strict_string(tx.get("input"), "input")),
                "value": rpc_quantity(tx.get("value")),
            }
        )
    if len(selected) > plan.max_router_transactions:
        raise ValueError("complete router receipt coverage cannot fit frozen bound")
    return tuple(selected)


def _receipt(plan: RouterActivityPlan, tx: dict[str, object], raw: object) -> dict[str, object]:
    r = strict_object(raw, "receipt")
    status = rpc_quantity(r.get("status"))
    if (
        _hash(r.get("transactionHash")) != tx["hash"]
        or rpc_quantity(r.get("transactionIndex")) != tx["index"]
        or _hash(r.get("blockHash")) != plan.header.ref.block_hash
        or rpc_quantity(r.get("blockNumber")) != plan.header.ref.number
        or _address(r.get("from")) != tx["from"]
        or _address(r.get("to")) != plan.router
        or status not in (0, 1)
    ):
        raise ValueError("router receipt identity/status differs")
    logs = strict_list(r.get("logs"), "receipt logs")
    if status == 0 and logs:
        raise ValueError("reverted router receipt carries logs")
    swaps = []
    previous = -1
    normalized_logs = []
    for item in logs:
        log = strict_object(item, "receipt log")
        position = rpc_quantity(log.get("logIndex"))
        if (
            position <= previous
            or _hash(log.get("blockHash")) != plan.header.ref.block_hash
            or rpc_quantity(log.get("blockNumber")) != plan.header.ref.number
            or _hash(log.get("transactionHash")) != tx["hash"]
            or rpc_quantity(log.get("transactionIndex")) != tx["index"]
            or type(log.get("removed", False)) is not bool
            or log.get("removed", False)
        ):
            raise ValueError("receipt logs changed, removed or reordered")
        previous = position
        record = LogRecord(
            _address(log.get("address")),
            tuple(_hash(t) for t in strict_list(log.get("topics"), "topics")),
            normalize_hex(strict_string(log.get("data"), "log data")),
            plan.header.ref.number,
            strict_string(tx["hash"], "transaction hash"),
            position,
            plan.header.ref.block_hash,
            int(tx["index"]),
            False,
        )
        normalized_logs.append(asdict(record))
        if record.address == plan.pool_manager and record.topics[:1] == (SWAP_TOPIC,):
            swap = decode_swap(record)
            if (
                not MIN_SQRT_PRICE <= swap.sqrt_price_x96 < MAX_SQRT_PRICE
                or not MIN_TICK <= swap.tick <= MAX_TICK
            ):
                raise ValueError("router Swap observation exceeds initialized pool bounds")
            if swap.fee is None:
                raise ValueError("router Swap observation lacks fee")
            validate_lp_fee(swap.fee)
            if swap.pool_id == plan.pool_id:
                swaps.append(
                    {
                        "log_index": position,
                        **asdict(swap),
                        "sender_is_router": swap.sender == plan.router,
                    }
                )
    return {**tx, "status": status, "logs": normalized_logs, "target_swaps": swaps}


@dataclass(frozen=True, slots=True)
class RouterActivityCapture:
    plan: RouterActivityPlan
    source: str
    exchanges: tuple[RpcExchange, ...]

    def facts(self) -> dict[str, object]:
        if self.source not in self.plan.providers or len(self.exchanges) < 5:
            raise ValueError("declared complete router source required")
        opening = successful_results(self.exchanges[:4], self.plan.opening())
        if opening[0] != hex(self.plan.header.ref.chain_id):
            raise ValueError("router activity chain differs")
        txs = router_transactions(self.plan, opening[1])
        expected = [
            *self.plan.opening(),
            *(("eth_getTransactionReceipt", [tx["hash"]]) for tx in txs),
            ("eth_getBlockByNumber", [hex(self.plan.header.ref.number), False]),
        ]
        results = successful_results(self.exchanges, expected)
        for exchange, (_, params) in zip(self.exchanges, expected, strict=True):
            actual, _ = decode_exchange(exchange)
            if canonical_json(actual) != canonical_json(params):
                raise ValueError("router activity exact canonical params required")
        require_code_hash(results[2], self.plan.pool_manager_runtime_keccak)
        require_code_hash(results[3], self.plan.router_runtime_keccak)
        closing = strict_object(results[-1], "closing header")
        if decode_header(closing, chain_id=self.plan.header.ref.chain_id) != self.plan.header:
            raise ValueError("router activity block moved")
        full = strict_object(results[1], "full block")
        tx_hashes = [
            _hash(strict_object(t, "transaction")["hash"])
            for t in strict_list(full["transactions"], "transactions")
        ]
        if strict_list(closing.get("transactions"), "closing transaction hashes") != tx_hashes:
            raise ValueError("full block transaction enumeration changed")
        receipts = [_receipt(self.plan, tx, r) for tx, r in zip(txs, results[4:-1], strict=True)]
        positions = [
            strict_integer(strict_object(log, "derived log")["log_index"], "derived index")
            for r in receipts
            for log in strict_list(r["logs"], "derived logs")
        ]
        if positions != sorted(set(positions)):
            raise ValueError("router receipts repeat or reorder block log positions")
        return {
            "schema": "oasis.router-block-activity.v1",
            "block": asdict(self.plan.header.ref),
            "pool_manager": self.plan.pool_manager,
            "router": self.plan.router,
            "pool_id": self.plan.pool_id,
            "full_block_transaction_hashes": tx_hashes,
            "outer_router_receipts": receipts,
            "target_admitted": False,
            "execution_eligible": False,
            "settled_exit_depth": None,
            "net_pnl": None,
        }


def collect_router_activity(
    session: RecordedSession, plan: RouterActivityPlan
) -> RouterActivityCapture:
    transport, recorder, source = session
    if recorder.exchanges or source not in plan.providers:
        raise ValueError("fresh declared router activity session required")
    if transport.request(*plan.opening()[0]) != hex(plan.header.ref.chain_id):
        raise ValueError("router activity chain differs")
    block = transport.request(*plan.opening()[1])
    txs = router_transactions(plan, block)
    for method, params in plan.opening()[2:]:
        transport.request(method, params)
    for tx in txs:
        transport.request("eth_getTransactionReceipt", [tx["hash"]])
    transport.request("eth_getBlockByNumber", [hex(plan.header.ref.number), False])
    captured = RouterActivityCapture(plan, source, recorder.exchanges)
    captured.facts()
    return captured


def retain_router_activity(store: EvidenceStore, capture: RouterActivityCapture) -> EvidenceRef:
    facts = capture.facts()
    return store.put(
        "router_block_activity",
        canonical_json(facts),
        capture.plan.header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation=facts,
        schema_version=1,
    )


def replay_router_activity(
    store: EvidenceStore, ref: EvidenceRef, plan: RouterActivityPlan
) -> dict[str, object]:
    if (
        ref.kind != "router_block_activity"
        or ref.schema_version != 1
        or ref.block != plan.header.ref
    ):
        raise ValueError("router activity evidence identity differs")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise ValueError("router activity raw proofs missing")
    facts = RouterActivityCapture(
        plan, ref.source, restore_exchanges(record.request_payload, record.raw_response_payload)
    ).facts()
    if record.payload != canonical_json(facts) or record.interpretation_payload != canonical_json(
        facts
    ):
        raise ValueError("router activity retained facts differ from raw proofs")
    return facts
