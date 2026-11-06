"""Offline replay of a fresh two-header window around all settled receipts.

No acquisition or clock assertion is provided here. The opening and closing raw
responses recheck the same explicit fresh NAV height. Reorg disagreement refuses
the capture; retained wire provenance and provider honesty remain trust inputs.
"""

from dataclasses import asdict, dataclass

from ..evidence.exact_capture import decode_header, successful_results, wire_envelope
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, canonical_json, normalize_hash32
from ..rpc.transport import RpcExchange

KIND = "live_rearm_settlements"
MAX_ANCHORS = 256


def settlement_requests(
    head: BlockRef, anchors: tuple[dict[str, object], ...]
) -> list[tuple[str, list[object]]]:
    requests: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(head.number), False]),
    ]
    for anchor in anchors:
        number = anchor["number"]
        if type(number) is not int:
            raise ValueError("integer settlement height required")
        requests.extend(
            [
                ("eth_getBlockByNumber", [hex(number), False]),
                ("eth_getTransactionReceipt", [anchor["transaction_hash"]]),
            ]
        )
    requests.append(("eth_getBlockByNumber", [hex(head.number), False]))
    return requests


def _opening_header(
    head: BlockRef, values: tuple[object, ...], nav_header: BlockSnapshot | None
) -> BlockSnapshot:
    opening = decode_header(values[1], chain_id=head.chain_id)
    if rpc_quantity(values[0]) != head.chain_id or opening.ref != head:
        raise ValueError("settlement pinned head moved or differs from NAV")
    if nav_header is not None and opening != nav_header:
        raise ValueError("settlement header differs from NAV")
    return opening


def require_settlement_opening(
    exchanges: tuple[RpcExchange, ...], nav_header: BlockSnapshot
) -> None:
    prefix = settlement_requests(nav_header.ref, ())[:2]
    _opening_header(nav_header.ref, successful_results(exchanges, prefix), nav_header)


@dataclass(frozen=True, slots=True)
class SettlementHeadCapture:
    head: BlockRef
    anchors: tuple[dict[str, object], ...]
    source: str
    exchanges: tuple[RpcExchange, ...]

    def reconstruct(
        self, *, nav_header: BlockSnapshot | None = None
    ) -> tuple[dict[str, object], ...]:
        head = self.head
        if (
            head.timestamp is None
            or not self.source
            or len(self.anchors) > MAX_ANCHORS
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 20_000_000
            or any(x.attempt != 1 for x in self.exchanges)
        ):
            raise ValueError("bounded single-attempt settlement window required")
        transactions = set()
        for anchor in self.anchors:
            if (
                set(anchor) != {"number", "hash", "transaction_hash"}
                or type(anchor["number"]) is not int
                or not 0 <= anchor["number"] <= head.number
                or not isinstance(anchor["hash"], str)
                or not isinstance(anchor["transaction_hash"], str)
                or anchor["transaction_hash"] in transactions
            ):
                raise ValueError("invalid or duplicate settlement anchor")
            normalize_hash32(anchor["hash"])
            normalize_hash32(anchor["transaction_hash"])
            transactions.add(anchor["transaction_hash"])
        values = successful_results(self.exchanges, settlement_requests(head, self.anchors))
        opening = _opening_header(head, values, nav_header)
        if decode_header(values[-1], chain_id=head.chain_id) != opening:
            raise ValueError("settlement pinned head moved or differs from NAV")
        receipts: list[dict[str, object]] = []
        for i, anchor in enumerate(self.anchors):
            block = decode_header(values[2 + 2 * i], chain_id=head.chain_id)
            receipt = values[3 + 2 * i]
            if (
                block.ref.number != anchor["number"]
                or block.ref.block_hash != anchor["hash"]
                or block.ref.timestamp is None
                or block.ref.timestamp > head.timestamp
                or (block.ref.number == head.number and block != opening)
                or not isinstance(receipt, dict)
                or receipt.get("blockHash") != anchor["hash"]
                or rpc_quantity(receipt.get("blockNumber")) != anchor["number"]
                or receipt.get("transactionHash") != anchor["transaction_hash"]
            ):
                raise ValueError("settled header or receipt is no longer canonical")
            receipts.append(receipt)
        return tuple(receipts)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.live-rearm-settlements.v1",
                "head": asdict(self.head),
                "anchors": self.anchors,
                "source": self.source,
                "receipts": self.reconstruct(),
                "execution_eligible": False,
            }
        )


def persist_settlement_head(store: EvidenceStore, capture: SettlementHeadCapture) -> EvidenceRef:
    """Retain supplied wire bytes only; this function cannot make an RPC request."""
    return store.put(
        KIND,
        capture.payload,
        capture.head,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={"execution_eligible": False, "scope": "pinned_window_settled_receipts"},
    )


def replay_settlement_head(
    store: EvidenceStore,
    ref: EvidenceRef,
    *,
    head: BlockRef,
    anchors: tuple[dict[str, object], ...],
) -> SettlementHeadCapture:
    if ref.kind != KIND or ref.block != head:
        raise ValueError("settlement window scope mismatch")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise ValueError("settlement window wire evidence missing")
    capture = SettlementHeadCapture(
        head,
        anchors,
        ref.source,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload:
        raise ValueError("settlement interpretation differs from retained bytes")
    return capture


def require_current_settlements(
    store: EvidenceStore,
    refs: tuple[EvidenceRef, ...],
    *,
    sources: tuple[str, str],
    nav_header: BlockSnapshot,
    anchors: tuple[dict[str, object], ...],
    historical: tuple[dict[str, object], ...],
) -> None:
    """Current raw responses must preserve every already reconciled receipt byte."""
    if len(sources) != 2 or sources[0] == sources[1] or tuple(r.source for r in refs) != sources:
        raise ValueError("fresh settlements require the NAV provider pair")
    for ref in refs:
        capture = replay_settlement_head(store, ref, head=nav_header.ref, anchors=anchors)
        if capture.reconstruct(nav_header=nav_header) != historical:
            raise ValueError("fresh receipt differs from original settled cash or gas")
