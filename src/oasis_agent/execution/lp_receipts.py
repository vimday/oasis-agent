"""Turn one observed receipt into the exact native declaration the next admission needs.

An admitted request spends native gas, and the ledger explains a native decrease only
when it equals the declared gas exactly: anything else is an unresolved operation and
refuses the whole frame. This module is the producer the broadcast gate already assumes
exists. It takes a transaction the operator's broadcast journal already sent, reads its
receipt from two independent providers, reconstructs the *whole* native cost of that
transaction (execution, L1 data and operator components included) from retained raw
bytes, and turns it into the `gas_units` the next reconciliation must declare.

It holds no key, signs nothing, broadcasts nothing and cannot alter a receipt. The block
is deliberately not an operator input: it is read from the receipt and pinned, so a
declared height can never disagree with the receipt that is supposed to be inside it,
and the second provider's capture fails closed if it does not see that same block. A
missing receipt, a provider disagreement, a chain without an explicit cost policy, a
payer that is not the reconciled account and a native asset whose decimals differ from
the cost policy are all refused; nothing here substitutes a zero cost.

Non-gas native movement is never derived. An account that also sent native value keeps an
unexplained residual and the next frame is refused, rather than having that outflow
absorbed into a declaration that would report it as gas.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ..evidence.exact_capture import RecordedSession, decode_header, validate_session_pair
from ..evidence.rpc_validation import decode_exchange, rpc_quantity
from ..evidence.store import EvidenceStore
from ..farming.transaction_costs import (
    TransactionCost,
    TransactionCostCapture,
    persist_transaction_cost,
    transaction_cost_head,
    transaction_cost_tail,
)
from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, normalize_address, normalize_hash32
from ..risk.portfolio_manifest import ChainInventory, PortfolioManifest
from .account_capture import SessionFactory

# The components two providers must agree on. Chain id, transaction hash and native
# decimals are excluded because the two captures share the same pin and hash by
# construction, so agreement on them is structural rather than informative.
_COST_FIELDS = (
    "payer",
    "status",
    "execution_native",
    "l1_native",
    "blob_native",
    "operator_native",
    "total_native",
)


@dataclass(frozen=True, slots=True)
class ReceiptObservation:
    """One broadcast transaction's cost, agreed by two providers and retained."""

    account_key: str
    chain_id: int
    transaction_hash: str
    payer: str
    status: int
    block_number: int
    block_hash: str
    total_native: int
    evidence: tuple[EvidenceRef, EvidenceRef]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "transaction_hash", normalize_hash32(self.transaction_hash))
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash))
        object.__setattr__(self, "payer", normalize_address(self.payer))
        if type(self.chain_id) is not int or not 0 < self.chain_id < 2**256:
            raise ValueError("receipt observation requires an explicit chain")
        if type(self.block_number) is not int or self.block_number < 0:
            raise ValueError("receipt observation requires a block number")
        if self.status not in (0, 1):
            raise ValueError("receipt observation status must be 0 or 1")
        if type(self.total_native) is not int or self.total_native < 0:
            raise ValueError("receipt observation native cost must be nonnegative")
        if len(self.evidence) != 2 or len({ref.source for ref in self.evidence}) != 2:
            raise ValueError("receipt observation requires two independent proofs")
        if any(ref.kind != "transaction_cost" for ref in self.evidence):
            raise ValueError("receipt observation must retain cost evidence")


def _receipt_object(value: object) -> dict[str, object]:
    """A receipt is either present and an object, or the transaction is not observed."""
    if value is None:
        raise ValueError("transaction has no observed receipt")
    if not isinstance(value, dict):
        raise ValueError("receipt result is not an object")
    return value


def _probe_header(
    session: RecordedSession, *, chain_id: int, transaction_hash: str
) -> BlockSnapshot:
    """Locate the transaction's own block and pin it before any cost call is made.

    The receipt names its block, so the block is a chain fact rather than an operator
    claim; the header is then read at that exact height and the receipt must point back
    at it. The lookup is not retained: evidence starts at the exact cost sequence.
    """
    transport = session[0]
    receipt = _receipt_object(transport.request("eth_getTransactionReceipt", [transaction_hash]))
    number = rpc_quantity(receipt.get("blockNumber"))
    header = decode_header(
        transport.request("eth_getBlockByNumber", [hex(number), False]), chain_id=chain_id
    )
    receipt_block = receipt.get("blockHash")
    if not isinstance(receipt_block, str):
        raise ValueError("receipt block hash is not a hash string")
    if header.ref.number != number or header.ref.block_hash != normalize_hash32(
        receipt_block, "receipt block hash"
    ):
        raise ValueError("receipt does not belong to the pinned block")
    return header


def _capture(
    session: RecordedSession, header: BlockSnapshot, transaction_hash: str
) -> TransactionCostCapture:
    """Issue the exact cost sequence and rebuild it from the exchanges it produced.

    The gas needed to build the OP tail is read from this session's own receipt, so the
    request sequence and the reconstruction share one definition of the wire protocol.
    """
    transport, recorder, source = session
    for method, params in transaction_cost_head(header.ref, transaction_hash):
        transport.request(method, params)
    receipt = _receipt_object(decode_exchange(recorder.exchanges[2])[1].get("result"))
    gas = rpc_quantity(receipt.get("gasUsed"))
    for method, params in transaction_cost_tail(header.ref, gas=gas):
        transport.request(method, params)
    return TransactionCostCapture(header, transaction_hash, source, recorder.exchanges)


def observe_receipt(
    store: EvidenceStore,
    entry: ChainInventory,
    *,
    transaction_hash: str,
    sessions: SessionFactory,
) -> ReceiptObservation:
    """Reconstruct one broadcast transaction's native cost from two providers.

    The provider that locates the block is not privileged: its answer only pins the
    height and hash, and the second provider's capture fails closed when it does not see
    the same block. Only exact agreement on every cost component becomes a declaration,
    and nothing is retained until every check has passed.
    """
    digest = normalize_hash32(transaction_hash)
    header = _probe_header(
        sessions(entry.primary_source), chain_id=entry.chain_id, transaction_hash=digest
    )
    first = sessions(entry.primary_source)
    second = sessions(entry.secondary_source)
    validate_session_pair(first, second)
    captures = (_capture(first, header, digest), _capture(second, header, digest))
    costs = (captures[0].reconstruct(), captures[1].reconstruct())
    if costs[0] != costs[1]:
        differing = [
            name for name in _COST_FIELDS if getattr(costs[0], name) != getattr(costs[1], name)
        ]
        raise ValueError("providers disagree on the receipt cost: " + ", ".join(differing))
    cost: TransactionCost = costs[0]
    if cost.payer != entry.account:
        raise ValueError("receipt payer is not the reconciled account")
    if entry.native.decimals != cost.native_decimals:
        raise ValueError("native asset decimals differ from the cost policy")
    return ReceiptObservation(
        entry.key,
        entry.chain_id,
        digest,
        cost.payer,
        cost.status,
        header.ref.number,
        header.ref.block_hash,
        cost.total_native,
        (
            persist_transaction_cost(store, captures[0]),
            persist_transaction_cost(store, captures[1]),
        ),
    )


def gas_declarations(
    manifest: PortfolioManifest, receipts: Sequence[ReceiptObservation]
) -> dict[str, dict[str, int]]:
    """The declaration the next reconciliation needs, covering exactly the manifest.

    One quantity is derived: the whole gas cost of each named transaction, summed per
    account. Non-gas native movement stays zero, so a receipt can never explain away a
    native outflow that bought something and lost value on the way.
    """
    if not isinstance(manifest, PortfolioManifest):
        raise TypeError("typed portfolio manifest required")
    totals = dict.fromkeys(manifest.keys, 0)
    seen: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, ReceiptObservation):
            raise TypeError("typed receipt observation required")
        if receipt.transaction_hash in seen:
            raise ValueError("one observation per transaction required")
        seen.add(receipt.transaction_hash)
        if receipt.account_key not in totals:
            raise ValueError("receipt account is not a declared manifest account")
        totals[receipt.account_key] += receipt.total_native
    return {key: {"gas_units": totals[key], "non_trading_units": 0} for key in manifest.keys}


__all__ = [
    "ReceiptObservation",
    "gas_declarations",
    "observe_receipt",
]
