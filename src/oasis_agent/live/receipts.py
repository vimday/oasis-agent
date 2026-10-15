"""Bind live receipt settlement to retained two-provider whole native gas costs.

This supplies gas evidence only. It does not prove wallet cash, LP settlement,
economic PnL or permission to submit the next transaction.
"""

from dataclasses import dataclass
from typing import Any

from ..evidence.rpc_validation import decode_exchange
from ..evidence.store import EvidenceStore
from ..execution.account_capture import SessionFactory
from ..execution.lp_receipts import collect_receipt_cost_quorum
from ..farming.transaction_costs import TransactionCost, persist_transaction_cost
from ..models.common import EvidenceRef, normalize_address, normalize_hash32


@dataclass(frozen=True, slots=True)
class ReceiptCostProof:
    cost: TransactionCost
    evidence: tuple[EvidenceRef, EvidenceRef]


class RetainedReceiptCostReader:
    """Read-only chain acquisition; persist proofs before returning any native cost."""

    def __init__(
        self,
        store: EvidenceStore,
        *,
        chain_id: int,
        account: str,
        native_decimals: int,
        sources: tuple[str, str],
        sessions: SessionFactory,
    ) -> None:
        if type(chain_id) is not int or chain_id <= 0 or native_decimals != 18:
            raise ValueError("explicit chain and 18-decimal native cost scope required")
        if len(sources) != 2 or sources[0] == sources[1]:
            raise ValueError("two distinct receipt provider sources required")
        self.store, self.chain_id = store, chain_id
        self.account, self.native_decimals = normalize_address(account), native_decimals
        self.sources, self.sessions = sources, sessions

    def observe(
        self, receipt: dict[str, Any], *, transaction_hash: str, target: str
    ) -> ReceiptCostProof:
        """The newly captured receipts must be the receipt the loop will apply.

        Comparing logs as well as gas/identity prevents proof for one observation
        being attached to a different mint/burn interpretation. Fresh acquisition
        rechecks chain and the closing header through the shared cost producer.
        """
        digest, destination = normalize_hash32(transaction_hash), normalize_address(target)
        if (
            receipt.get("transactionHash") != digest
            or receipt.get("from") != self.account
            or receipt.get("to") != destination
        ):
            raise ValueError("cost receipt differs from pending transaction identity")
        captures = collect_receipt_cost_quorum(
            chain_id=self.chain_id,
            transaction_hash=digest,
            sources=self.sources,
            sessions=self.sessions,
        )
        cost = captures[0].reconstruct()
        if cost.payer != self.account or cost.native_decimals != self.native_decimals:
            raise ValueError("cost receipt payer or native units differ from live account")
        fields = (
            "transactionHash",
            "blockHash",
            "blockNumber",
            "from",
            "to",
            "status",
            "gasUsed",
            "effectiveGasPrice",
            "logs",
            "type",
            "l1Fee",
            "operatorFeeScalar",
            "operatorFeeConstant",
            "gasUsedForL1",
            "blobGasUsed",
            "blobGasPrice",
        )
        for capture in captures:
            observed = decode_exchange(capture.exchanges[2])[1]["result"]
            if not isinstance(observed, dict) or any(
                (name in observed) != (name in receipt) or observed.get(name) != receipt.get(name)
                for name in fields
            ):
                raise ValueError("retained cost receipt differs from live settlement receipt")
        evidence = (
            persist_transaction_cost(self.store, captures[0]),
            persist_transaction_cost(self.store, captures[1]),
        )
        # Read back through the store's integrity check before the caller mutates
        # pending. A persistence failure leaves settlement unresolved.
        for ref in evidence:
            self.store.get_record(ref)
        return ReceiptCostProof(cost, evidence)
