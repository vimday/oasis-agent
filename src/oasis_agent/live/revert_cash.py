"""EOA reverted-action cash conservation, never a successful LP cash adapter.

An empty-code EOA with exactly one nonce consumed by its own reverted
transaction can lose native currency only to that transaction's gas. Require
that exact debit, unchanged USDC, and no USDC/NFT owner Transfer anywhere in a
short, fully parent-linked block interval. Offsetting token flows are refused.
"""

import json
from dataclasses import asdict, dataclass

from ..evidence.exact_capture import (
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges
from ..evidence.store import EvidenceStore
from ..execution.account_capture import SessionFactory
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    evidence_ref_from_json,
    normalize_address,
)
from ..rpc.transport import RpcExchange
from .cash_interval import MAX_CASH_BLOCKS, TransferFreeInterval, capture_interval, gas_only_wallets
from .config import LiveConfig
from .intent import SubmittedTransaction
from .wallet import WalletBalance, capture_wallet, replay_wallet

MAX_REVERT_BLOCKS = MAX_CASH_BLOCKS


@dataclass(frozen=True, slots=True)
class RevertCashCapture:
    transaction: SubmittedTransaction
    opening: EvidenceRef
    closing: EvidenceRef
    cost: EvidenceRef
    headers: tuple[BlockSnapshot, ...]
    exchanges: tuple[RpcExchange, ...]

    def reconstruct(self, store: EvidenceStore, config: LiveConfig) -> WalletBalance:
        a, b = gas_only_wallets(
            store,
            config,
            self.transaction,
            self.opening,
            self.closing,
            self.cost,
            expected_status=0,
        )
        TransferFreeInterval(a.header, self.headers, self.exchanges).validate(config, b.header)
        return b

    def payload(self, store: EvidenceStore, config: LiveConfig) -> bytes:
        balance = self.reconstruct(store, config)
        return canonical_json(
            {
                "schema": "oasis.live-revert-cash.v1",
                "transaction": asdict(self.transaction),
                "opening": asdict(self.opening),
                "closing": asdict(self.closing),
                "cost": asdict(self.cost),
                "headers": [asdict(h) for h in self.headers],
                "position_manager": normalize_address(config.position_manager),
                "balance": asdict(balance),
                "execution_eligible": False,
            }
        )


def replay_revert_cash(
    store: EvidenceStore, ref: EvidenceRef, config: LiveConfig
) -> RevertCashCapture:
    record = store.get_record(ref)
    if ref.kind != "live_revert_cash" or record.request_payload is None:
        raise ValueError("revert cash evidence scope missing")
    data = json.loads(record.payload)
    capture = RevertCashCapture(
        SubmittedTransaction(**data["transaction"]),
        evidence_ref_from_json(data["opening"]),
        evidence_ref_from_json(data["closing"]),
        evidence_ref_from_json(data["cost"]),
        tuple(BlockSnapshot(BlockRef(**h["ref"]), h["parent_hash"]) for h in data["headers"]),
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if (
        ref.source != capture.opening.source
        or ref.block != capture.closing.block
        or capture.payload(store, config) != record.payload
    ):
        raise ValueError("revert cash interpretation differs from raw proof")
    return capture


def reconcile_revert(
    store: EvidenceStore,
    config: LiveConfig,
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
        raise ValueError("revert requires the declared pair of opening and cost proofs")
    starts = tuple(replay_wallet(store, r) for r in opening)
    if starts[0].reconstruct() != starts[1].reconstruct():
        raise ValueError("revert opening providers disagree")
    start, end = starts[0].block, costs[0].block
    if not 0 < end.number - start.number <= MAX_REVERT_BLOCKS:
        raise ValueError("revert cash interval outside bound")
    closing = capture_wallet(store, config, end, sources=sources, sessions=sessions)
    pair = (sessions(sources[0]), sessions(sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != sources:
        raise ValueError("revert window aliases changed")
    captures = []
    for index, session in enumerate(pair):
        headers = capture_interval(config, start, end, session)
        recorder = session[1]
        capture = RevertCashCapture(
            transaction,
            opening[index],
            closing[index],
            costs[index],
            tuple(headers),
            recorder.exchanges,
        )
        capture.reconstruct(store, config)
        captures.append(capture)
    if (
        captures[0].reconstruct(store, config) != captures[1].reconstruct(store, config)
        or captures[0].headers != captures[1].headers
    ):
        raise ValueError("revert cash providers disagree")
    refs = [
        store.put(
            "live_revert_cash",
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
        replay_revert_cash(store, ref, config)
    return captures[0].reconstruct(store, config), (refs[0], refs[1])
