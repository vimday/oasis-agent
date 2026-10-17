"""Successful swap/mint/burn cash from bounded two-provider raw proofs.

Native owner movements come from a complete callTracer tree, never from a
balance delta. The delta must independently equal those movements minus full gas.
Unsupported traces, unrelated transfers and all unexplained residuals refuse.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

from eth_abi.abi import decode
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..execution.account_capture import SessionFactory
from ..farming.transaction_costs import replay_transaction_cost
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    evidence_ref_from_json,
    normalize_address,
    sha256_hex,
)
from ..rpc.transport import RpcExchange
from .action_plan import action_call
from .cash_interval import MAX_CASH_BLOCKS, TRANSFER, _logs, capture_interval
from .config import LiveConfig
from .intent import SubmittedTransaction
from .state import Position
from .wallet import WalletBalance, WalletScope, capture_wallet, replay_wallet


def native_delta(root: object, tx: SubmittedTransaction, manager: str, data: bytes) -> int:
    if not isinstance(root, dict) or root.get("type") != "CALL" or root.get("error"):
        raise ValueError("complete successful call tree required")
    if (root.get("from"), root.get("to"), root.get("input")) != (
        tx.account,
        tx.target,
        "0x" + data.hex(),
    ) or rpc_quantity(root.get("value")) != tx.value:
        raise ValueError("call tree is not the signed action")
    stack: list[tuple[dict[str, Any], str | None, int]] = [(root, None, 0)]
    delta = count = 0
    while stack:
        frame, context, depth = stack.pop()
        count += 1
        if count > 10_000 or depth > 64:
            raise ValueError("trace exceeds structural budget")
        kind = frame.get("type")
        if kind not in {"CALL", "STATICCALL", "DELEGATECALL"}:
            raise ValueError("unsupported trace frame")
        sender, recipient = normalize_address(frame["from"]), normalize_address(frame["to"])
        if context is not None and sender != context:
            raise ValueError("trace caller linkage differs")
        if "error" in frame:
            if not isinstance(frame["error"], str) or not frame["error"]:
                raise ValueError("invalid trace error")
            continue  # All descendants of a reverted call are rolled back as well.
        value = rpc_quantity(frame.get("value") if kind == "CALL" else frame.get("value", "0x0"))
        if kind == "STATICCALL" and value:
            raise ValueError("static call cannot transfer native value")
        if kind == "CALL":
            if sender == tx.account:
                if depth != 0 or recipient != tx.target or value != tx.value:
                    raise ValueError("unexpected owner native debit")
                delta -= value
            if recipient == tx.account:
                if sender not in {tx.target, manager}:
                    raise ValueError("unexpected native refund payer")
                delta += value
        children = frame.get("calls", [])
        if not isinstance(children, list) or any(not isinstance(c, dict) for c in children):
            raise ValueError("incomplete trace children")
        child_context = context if kind == "DELEGATECALL" else recipient
        if child_context is None:
            raise ValueError("invalid delegate context")
        stack.extend((child, child_context, depth + 1) for child in children)
    return delta


def _extras(
    c: LiveConfig, end: BlockRef, tx: SubmittedTransaction
) -> list[tuple[str, list[object]]]:
    pin = {"blockHash": end.block_hash, "requireCanonical": True}
    return [
        ("eth_getCode", [c.state_view, pin]),
        (
            "eth_call",
            [{"to": c.state_view, "data": "0x" + keccak(b"poolManager()")[:4].hex()}, pin],
        ),
        (
            "debug_traceTransaction",
            [
                tx.transaction_hash,
                {"tracer": "callTracer", "timeout": "5s", "tracerConfig": {"onlyTopCall": False}},
            ],
        ),
        ("eth_getBlockByNumber", [hex(end.number), False]),
    ]


def _owner_logs(
    values: object, c: LiveConfig, header: BlockSnapshot, tx: SubmittedTransaction
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("bounded owner log array required")
    owner = "0x" + "0" * 24 + c.account[2:]
    result = []
    for log in values:
        if not isinstance(log, dict) or log.get("removed") is not False:
            raise ValueError("canonical owner log required")
        topics = log.get("topics")
        if (
            log.get("blockHash") != header.ref.block_hash
            or rpc_quantity(log.get("blockNumber")) != header.ref.number
            or log.get("transactionHash") != tx.transaction_hash
            or not isinstance(topics, list)
            or len(topics) not in (3, 4)
            or topics[0] != TRANSFER
            or owner not in topics[1:3]
            or log.get("address") not in {c.pool.currency1, c.position_manager}
        ):
            raise ValueError("foreign or malformed owner transfer")
        rpc_quantity(log.get("logIndex"))
        rpc_quantity(log.get("transactionIndex"))
        for topic in topics[1:3]:
            if not isinstance(topic, str) or len(topic) != 66 or topic[:26] != "0x" + "0" * 24:
                raise ValueError("noncanonical address topic")
        result.append(log)
    return result


@dataclass(frozen=True, slots=True)
class ActionCashCapture:
    kind: str
    intent: dict[str, Any]
    position: Position | None
    transaction: SubmittedTransaction
    opening: EvidenceRef
    closing: EvidenceRef
    cost: EvidenceRef
    headers: tuple[BlockSnapshot, ...]
    exchanges: tuple[RpcExchange, ...]

    def reconstruct(self, store: EvidenceStore, c: LiveConfig) -> WalletBalance:
        if self.kind not in {"swap", "mint", "burn"}:
            raise ValueError("cash proof requires a successful economic action")
        x, y = replay_wallet(store, self.opening), replay_wallet(store, self.closing)
        if (
            x.scope != y.scope
            or x.scope != WalletScope.from_config(c)
            or len({self.opening.source, self.closing.source, self.cost.source}) != 1
        ):
            raise ValueError("action cash scope/provider mismatch")
        a, b, tx = x.reconstruct(), y.reconstruct(), self.transaction
        if (tx.chain_id, tx.account) != (c.chain_id, c.account) or (a.nonce, b.nonce) != (
            tx.nonce,
            tx.nonce + 1,
        ):
            raise ValueError("action nonce or account mismatch")
        if a.token_runtime_hash != b.token_runtime_hash:
            raise ValueError("token runtime changed")
        if a.header.ref.timestamp is None or b.header.ref.timestamp is None:
            raise ValueError("action timestamps missing")
        target, data, value = action_call(
            c, a.header.ref.timestamp, self.kind, self.intent, self.position
        )
        if (tx.target, tx.value, tx.calldata_hash) != (target, value, "0x" + keccak(data).hex()):
            raise ValueError("cash intent differs from signed transaction")
        if b.header.ref.timestamp > a.header.ref.timestamp + 300:
            raise ValueError("action landed after its bound deadline")
        count = b.header.ref.number - a.header.ref.number
        if not 0 < count <= MAX_CASH_BLOCKS or len(self.headers) != count:
            raise ValueError("action interval gap or bound exceeded")
        if (
            sum(len(e.request_payload) + len(e.response_payload) for e in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("action cash byte budget exceeded")
        requests: list[tuple[str, list[object]]] = [
            ("eth_chainId", []),
            ("eth_getBlockByNumber", [hex(a.header.ref.number), False]),
        ]
        for header in self.headers:
            requests.append(("eth_getBlockByNumber", [hex(header.ref.number), False]))
            requests.extend(_logs(c, header.ref))
        requests.append(("eth_getBlockByNumber", [hex(b.header.ref.number), False]))
        requests.extend(_extras(c, b.header.ref, tx))
        vals = successful_results(self.exchanges, requests)
        if (
            rpc_quantity(vals[0]) != c.chain_id
            or decode_header(vals[1], chain_id=c.chain_id) != a.header
        ):
            raise ValueError("cash interval opening/chain changed")
        seen: dict[tuple[str, int], dict[str, Any]] = {}
        previous = a.header
        for i, header in enumerate(self.headers):
            if (
                decode_header(vals[2 + 3 * i], chain_id=c.chain_id) != header
                or header.ref.number != previous.ref.number + 1
                or header.parent_hash != previous.ref.block_hash
                or header.ref.timestamp is None
                or previous.ref.timestamp is None
                or header.ref.timestamp <= previous.ref.timestamp
            ):
                raise ValueError("cash interval is not parent linked")
            for side, entries in enumerate(vals[3 + 3 * i : 5 + 3 * i]):
                query_seen = set()
                for log in _owner_logs(entries, c, header, tx):
                    if log["topics"][side + 1] != "0x" + "0" * 24 + c.account[2:]:
                        raise ValueError("owner log violates its query filter")
                    key = (log["blockHash"], rpc_quantity(log["logIndex"]))
                    if key in query_seen:
                        raise ValueError("duplicate owner log within one query")
                    query_seen.add(key)
                    if key in seen and seen[key] != log:
                        raise ValueError("conflicting owner log")
                    seen[key] = log
            previous = header
        tail = 2 + 3 * count
        if previous != b.header or decode_header(vals[tail], chain_id=c.chain_id) != b.header:
            raise ValueError("cash closing block changed")
        require_code_hash(vals[tail + 1], c.expected_pins["state_view"])
        encoded = vals[tail + 2]
        if not isinstance(encoded, str) or len(encoded) != 66:
            raise ValueError("pool manager identity missing")
        manager = str(decode(["address"], bytes.fromhex(encoded[2:]))[0])
        native = native_delta(vals[tail + 3], tx, manager, data)
        if decode_header(vals[tail + 4], chain_id=c.chain_id) != b.header:
            raise ValueError("block moved during trace")
        capture = replay_transaction_cost(
            store, self.cost, header=b.header, transaction_hash=tx.transaction_hash
        )
        cost = capture.reconstruct()
        receipt = decode_exchange(capture.exchanges[2])[1]["result"]
        if not isinstance(receipt, dict):
            raise ValueError("receipt object missing")
        if cost.status != 1 or cost.payer != c.account or cost.native_decimals != 18:
            raise ValueError("successful action requires own complete gas proof")
        if (
            receipt["to"] != tx.target
            or rpc_quantity(receipt["gasUsed"]) > tx.gas_limit
            or rpc_quantity(receipt["effectiveGasPrice"]) > tx.max_fee_per_gas
        ):
            raise ValueError("receipt differs from signed gas/target")
        receipt_owner = _owner_logs(
            [
                log
                for log in receipt["logs"]
                if log.get("address") in {c.pool.currency1, c.position_manager}
                and isinstance(log.get("topics"), list)
                and len(log["topics"]) >= 3
                and ("0x" + "0" * 24 + c.account[2:]) in log["topics"][1:3]
                and log["topics"][0] == TRANSFER
            ],
            c,
            b.header,
            tx,
        )
        if sorted(canonical_json(v) for v in seen.values()) != sorted(
            canonical_json(v) for v in receipt_owner
        ):
            raise ValueError("receipt and complete interval owner logs differ")
        token, nfts = self._token_cash(c, manager, tuple(seen.values()))
        if (b.native, b.token) != (a.native + native - cost.total_native, a.token + token):
            raise ValueError("successful action wallet residual is unexplained")
        self._bounds(native, token, nfts)
        return b

    def _token_cash(
        self, c: LiveConfig, manager: str, logs: tuple[dict[str, Any], ...]
    ) -> tuple[int, list[tuple[str, str, int]]]:
        total, nfts = 0, []
        for log in logs:
            topics = log["topics"]
            sender, recipient = ("0x" + t[-40:] for t in topics[1:3])
            if log["address"] == c.pool.currency1:
                if len(topics) != 3 or len(log["data"]) != 66:
                    raise ValueError("noncanonical USDC transfer")
                amount = int(decode(["uint256"], bytes.fromhex(log["data"][2:]))[0])
                if (
                    sender != c.account
                    and sender not in {manager, c.position_manager, c.universal_router}
                ) or (
                    recipient != c.account
                    and recipient not in {manager, c.position_manager, c.universal_router}
                ):
                    raise ValueError("third party token movement")
                total += amount * ((recipient == c.account) - (sender == c.account))
            else:
                if len(topics) != 4 or log["data"] != "0x" or len(topics[3]) != 66:
                    raise ValueError("noncanonical NFT transfer")
                nfts.append((sender, recipient, int(topics[3], 16)))
        return total, nfts

    def _bounds(self, native: int, token: int, nfts: list[tuple[str, str, int]]) -> None:
        a, owner, zero = self.intent, self.transaction.account, "0x" + "0" * 40
        if self.kind == "swap":
            debit, credit = (-native, token) if a["zero_for_one"] else (-token, native)
            if debit != a["amount_in"] or credit < a["min_out"] or nfts:
                raise ValueError("swap cash violates exact input/minimum output")
        elif self.kind == "mint":
            if (
                not 0 <= -native <= a["amount0_max"]
                or not 0 <= -token <= a["amount1_max"]
                or len(nfts) != 1
                or nfts[0][:2] != (zero, owner)
                or nfts[0][2] <= 0
            ):
                raise ValueError("mint cash/NFT violates intended bounds")
        elif (
            self.position is None
            or native < a["amount0_min"]
            or token < a["amount1_min"]
            or nfts != [(owner, zero, self.position.token_id)]
        ):
            raise ValueError("burn cash/NFT violates intended bounds")

    def payload(self, store: EvidenceStore, config: LiveConfig) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.live-action-cash.v1",
                "kind": self.kind,
                "intent": self.intent,
                "position": asdict(self.position) if self.position else None,
                "transaction": asdict(self.transaction),
                "opening": asdict(self.opening),
                "closing": asdict(self.closing),
                "cost": asdict(self.cost),
                "headers": [asdict(h) for h in self.headers],
                "balance": asdict(self.reconstruct(store, config)),
                "config_hash": sha256_hex(canonical_json(asdict(config))),
                "execution_eligible": False,
            }
        )


def replay_action_cash(
    store: EvidenceStore, ref: EvidenceRef, config: LiveConfig
) -> ActionCashCapture:
    record = store.get_record(ref)
    if ref.kind != "live_action_cash" or record.request_payload is None:
        raise ValueError("action cash evidence scope missing")
    d = json.loads(record.payload)
    capture = ActionCashCapture(
        d["kind"],
        d["intent"],
        Position(**d["position"]) if d["position"] else None,
        SubmittedTransaction(**d["transaction"]),
        evidence_ref_from_json(d["opening"]),
        evidence_ref_from_json(d["closing"]),
        evidence_ref_from_json(d["cost"]),
        tuple(BlockSnapshot(BlockRef(**h["ref"]), h["parent_hash"]) for h in d["headers"]),
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if (
        ref.source != capture.opening.source
        or ref.block != capture.closing.block
        or capture.payload(store, config) != record.payload
    ):
        raise ValueError("action cash interpretation differs from raw proof")
    return capture


def reconcile_action(
    store: EvidenceStore,
    config: LiveConfig,
    kind: str,
    intent: dict[str, Any],
    position: Position | None,
    transaction: SubmittedTransaction,
    opening: tuple[EvidenceRef, ...],
    costs: tuple[EvidenceRef, ...],
    *,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[WalletBalance, tuple[EvidenceRef, EvidenceRef]]:
    if (
        len(opening) != 2
        or len(costs) != 2
        or tuple(r.source for r in opening) != sources
        or tuple(r.source for r in costs) != sources
        or costs[0].block is None
        or costs[0].block != costs[1].block
    ):
        raise ValueError("action cash requires declared opening/cost pair")
    starts = tuple(replay_wallet(store, r) for r in opening)
    if starts[0].reconstruct() != starts[1].reconstruct():
        raise ValueError("action opening quorum disagrees")
    start, end = starts[0].block, costs[0].block
    if not 0 < end.number - start.number <= MAX_CASH_BLOCKS or start.timestamp is None:
        raise ValueError("action interval outside bounded scope")
    action_call(config, start.timestamp, kind, intent, position)
    closing = capture_wallet(store, config, end, sources=sources, sessions=sessions)
    pair = (sessions(sources[0]), sessions(sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != sources:
        raise ValueError("action provider aliases changed")
    captures = []
    for i, session in enumerate(pair):
        headers = capture_interval(config, start, end, session)
        for method, params in _extras(config, end, transaction):
            session[0].request(method, params)
        captures.append(
            ActionCashCapture(
                kind,
                dict(intent),
                position,
                transaction,
                opening[i],
                closing[i],
                costs[i],
                headers,
                session[1].exchanges,
            )
        )
    if (
        captures[0].reconstruct(store, config) != captures[1].reconstruct(store, config)
        or captures[0].headers != captures[1].headers
    ):
        raise ValueError("action cash providers disagree")
    # Full deterministic call-tree agreement, including refunds, not just equal net balances.
    traces = [tuple(decode_exchange(e)[1]["result"] for e in c.exchanges[-3:-1]) for c in captures]
    if canonical_json(traces[0]) != canonical_json(traces[1]):
        raise ValueError("manager identity or call trees disagree")
    refs = [
        store.put(
            "live_action_cash",
            c.payload(store, config),
            end,
            c.opening.source,
            request=wire_envelope(c.exchanges, response=False),
            raw_response=wire_envelope(c.exchanges, response=True),
            interpretation={"execution_eligible": False},
        )
        for c in captures
    ]
    for ref in refs:
        replay_action_cash(store, ref, config)
    return captures[0].reconstruct(store, config), (refs[0], refs[1])
