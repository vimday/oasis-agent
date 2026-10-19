"""Replayable two-source transaction/trace/OP-cost capability acceptance.

This observes a historical transaction, never an instruction to trade. It does
not establish owner cash conservation, NAV, freshness or production eligibility.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..farming.transaction_costs import (
    TransactionCostCapture,
    transaction_cost_head,
    transaction_cost_tail,
)
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from ..rpc.transport import RpcExchange

SCHEMA = "oasis.live-provider-acceptance.v1"


def extra_requests(block: BlockRef, transaction: str) -> list[tuple[str, list[object]]]:
    return [
        ("eth_getTransactionByHash", [transaction]),
        (
            "debug_traceTransaction",
            [
                transaction,
                {
                    "tracer": "callTracer",
                    "timeout": "5s",
                    "tracerConfig": {"onlyTopCall": False},
                },
            ],
        ),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]


def trace_frames(trace: object, transaction: dict[str, Any]) -> int:
    if not isinstance(trace, dict) or trace.get("type") != "CALL" or trace.get("error"):
        raise ValueError("successful full call tree required")
    if any(trace.get(k) != transaction.get(k) for k in ("from", "to", "input")):
        raise ValueError("trace root differs from transaction")
    calldata = transaction.get("input")
    if not isinstance(calldata, str) or not calldata.startswith("0x") or len(calldata) % 2:
        raise ValueError("canonical transaction input required")
    bytes.fromhex(calldata[2:])
    if rpc_quantity(trace.get("value")) != rpc_quantity(transaction.get("value")):
        raise ValueError("trace value differs from transaction")
    stack = [(trace, 0)]
    count = 0
    while stack:
        frame, depth = stack.pop()
        count += 1
        if depth > 64 or count > 10_000 or not isinstance(frame, dict):
            raise ValueError("trace structure exceeds bounds")
        if frame.get("type") not in {
            "CALL",
            "STATICCALL",
            "DELEGATECALL",
            "CALLCODE",
            "CREATE",
            "CREATE2",
            "SELFDESTRUCT",
        }:
            raise ValueError("unknown trace frame")
        normalize_address(frame["from"])
        # A failed CREATE may have no destination; do not invent one.
        if "to" in frame:
            normalize_address(frame["to"])
        elif frame["type"] not in {"CREATE", "CREATE2"} or not frame.get("error"):
            raise ValueError("trace destination missing")
        for name in ("value", "gas", "gasUsed"):
            if name in frame:
                rpc_quantity(frame[name])
        children = frame.get("calls", [])
        if not isinstance(children, list):
            raise ValueError("trace children missing")
        stack.extend((child, depth + 1) for child in children)
    if count < 2:
        raise ValueError("nested call capability is not established")
    return count


@dataclass(frozen=True, slots=True)
class ProviderCapture:
    block: BlockRef
    transaction: str
    source: str
    exchanges: tuple[RpcExchange, ...]

    def reconstruct(self) -> dict[str, object]:
        if self.block.chain_id != 8453 or self.block.timestamp is None:
            raise ValueError("timestamped Base block required")
        if normalize_hash32(self.transaction) != self.transaction or not self.source:
            raise ValueError("canonical transaction and source required")
        if (
            len(self.exchanges) != 9
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 10_000_000
        ):
            raise ValueError("incomplete or oversized provider evidence")
        header = decode_header(decode_exchange(self.exchanges[1])[1]["result"], chain_id=8453)
        if header.ref != self.block:
            raise ValueError("provider block differs from scope")
        cost = TransactionCostCapture(
            header, self.transaction, self.source, self.exchanges[:6]
        ).reconstruct()
        receipt = decode_exchange(self.exchanges[2])[1]["result"]
        if not isinstance(receipt, dict):
            raise ValueError("receipt missing")
        expected = (
            transaction_cost_head(self.block, self.transaction)
            + transaction_cost_tail(self.block, gas=rpc_quantity(receipt["gasUsed"]))
            + extra_requests(self.block, self.transaction)
        )
        values = successful_results(self.exchanges, expected)
        transaction, trace, close = values[6:]
        if not isinstance(transaction, dict) or (
            transaction.get("hash") != self.transaction
            or transaction.get("blockHash") != self.block.block_hash
            or rpc_quantity(transaction.get("blockNumber")) != self.block.number
            or transaction.get("from") != receipt.get("from")
            or transaction.get("to") != receipt.get("to")
            or decode_header(close, chain_id=8453) != header
            or cost.status != 1
        ):
            raise ValueError("transaction/receipt/closing block mismatch")
        count = trace_frames(trace, transaction)
        return {
            "schema": SCHEMA,
            "block": asdict(self.block),
            "transaction": self.transaction,
            "transaction_content_hash": sha256_hex(canonical_json(transaction)),
            "trace_hash": sha256_hex(canonical_json(trace)),
            "trace_frames": count,
            "cost": asdict(cost),
            "execution_eligible": False,
        }


def collect(session: RecordedSession, block: BlockRef, transaction: str) -> ProviderCapture:
    rpc, recorder, source = session
    if recorder.exchanges:
        raise ValueError("fresh provider session required")
    if (
        block.chain_id != 8453
        or block.timestamp is None
        or normalize_hash32(transaction) != transaction
    ):
        raise ValueError("timestamped Base block and canonical transaction required")
    values = [rpc.request(m, p) for m, p in transaction_cost_head(block, transaction)]
    if not isinstance(values[2], dict):
        raise ValueError("receipt missing")
    for method, params in transaction_cost_tail(
        block, gas=rpc_quantity(values[2]["gasUsed"])
    ) + extra_requests(block, transaction):
        rpc.request(method, params)
    capture = ProviderCapture(block, transaction, source, recorder.exchanges)
    capture.reconstruct()
    return capture


def collect_quorum(
    store: EvidenceStore,
    first: RecordedSession,
    second: RecordedSession,
    block: BlockRef,
    transaction: str,
) -> tuple[EvidenceRef, EvidenceRef]:
    validate_session_pair(first, second)
    captures = [collect(session, block, transaction) for session in (first, second)]
    payloads = [canonical_json(c.reconstruct()) for c in captures]
    if payloads[0] != payloads[1]:
        raise ValueError("providers disagree on transaction, complete trace or fees")
    refs = []
    for capture, payload in zip(captures, payloads, strict=True):
        refs.append(
            store.put(
                "live_provider_acceptance",
                payload,
                block,
                capture.source,
                request=wire_envelope(capture.exchanges, response=False),
                raw_response=wire_envelope(capture.exchanges, response=True),
                interpretation={"execution_eligible": False, "schema": SCHEMA},
            )
        )
    return refs[0], refs[1]


def replay(store: EvidenceStore, ref: EvidenceRef) -> dict[str, object]:
    if ref.kind != "live_provider_acceptance" or ref.block is None:
        raise ValueError("provider evidence kind/block required")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise ValueError("retained provider requests missing")
    data = json.loads(record.payload)
    capture = ProviderCapture(
        ref.block,
        data["transaction"],
        ref.source,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    actual = capture.reconstruct()
    if canonical_json(actual) != record.payload:
        raise ValueError("provider interpretation differs from retained wire")
    return actual
