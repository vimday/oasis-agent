"""Compare wallet movement claims with fixed balances and selected receipt logs."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal

from ..evidence.public_receipts import ObservedReceipt
from ..evidence.rpc_validation import strict_list, strict_object
from .aave_cashflows import transfer
from .cycle_plan import address, hash32

Kind = Literal["erc20", "external", "internal"]


def decimal_quantity(value: object, bits: int = 256) -> int:
    if (
        not isinstance(value, str)
        or len(value) > 78
        or re.fullmatch(r"0|[1-9][0-9]*", value) is None
        or int(value) >= 1 << bits
    ):
        raise ValueError("invalid_wallet_decimal_quantity")
    return int(value)


@dataclass(frozen=True, slots=True)
class Movement:
    kind: Kind
    block: int
    block_hash: str | None
    transaction: str
    transaction_index: int | None
    sender: str
    recipient: str
    value_raw: int
    token: str | None
    trace_id: str | None
    success: bool
    gas_paid_raw: int

    def delta(self, account: str) -> int:
        return self.value_raw * ((self.recipient == account) - (self.sender == account))


def index_rows(
    kind: Kind,
    response: object,
    account: str,
    start: int,
    end: int,
    token: tuple[str, int] | None = None,
) -> tuple[Movement, ...]:
    envelope = strict_object(response, "wallet index")
    rows = strict_list(envelope.get("result"), "wallet index rows")
    if len(rows) >= 200 or (
        envelope.get("status") != "1"
        and not (
            envelope.get("status") == "0"
            and not rows
            and envelope.get("message") in {"No transactions found", "No records found"}
        )
    ):
        raise ValueError("wallet_index_full_page_or_failure")
    decoded = []
    for item in rows:
        row = strict_object(item, "wallet index row")
        block = decimal_quantity(row.get("blockNumber"), 64)
        sender, recipient = address(row.get("from")), address(row.get("to"))
        if not start <= block <= end or account not in (sender, recipient):
            raise ValueError("wallet_index_row_outside_filter")
        success, gas = True, 0
        trace: str | None = None
        block_hash, tx_index = None, None
        if kind != "internal":
            block_hash = hash32(row.get("blockHash"))
            tx_index = decimal_quantity(row.get("transactionIndex"), 32)
        if kind == "erc20":
            observed = (
                address(row.get("contractAddress")),
                decimal_quantity(row.get("tokenDecimal"), 8),
            )
            if token != observed:
                raise ValueError("wallet_index_token_or_decimals_mismatch")
        else:
            flag = row.get("isError")
            if flag not in ("0", "1"):
                raise ValueError("invalid_wallet_index_success_flag")
            success = flag == "0"
        if kind == "external":
            if row.get("txreceipt_status") != ("1" if success else "0"):
                raise ValueError("wallet_index_receipt_status_mismatch")
            gas = decimal_quantity(row.get("gasUsed")) * decimal_quantity(row.get("gasPrice"))
            if gas >= 1 << 256:
                raise ValueError("wallet_index_gas_overflow")
        if kind == "internal":
            trace_claim = row.get("traceId")
            if (
                row.get("type") != "call"
                or not isinstance(trace_claim, str)
                or re.fullmatch(r"[0-9_]{1,80}", trace_claim) is None
            ):
                raise ValueError("unsupported_wallet_index_trace")
            trace = trace_claim
        decoded.append(
            Movement(
                kind,
                block,
                block_hash,
                hash32(row.get("hash")),
                tx_index,
                sender,
                recipient,
                decimal_quantity(row.get("value")),
                token[0] if token else None,
                trace,
                success,
                gas,
            )
        )
    return tuple(decoded)


def reconcile_wallet(
    account: str,
    opening: dict[str | None, int],
    closing: dict[str | None, int],
    index: tuple[Movement, ...],
    receipts: tuple[ObservedReceipt, ...],
) -> dict[str, object]:
    if set(opening) != set(closing) or None not in opening:
        raise ValueError("wallet_boundary_assets_mismatch")
    actual: dict[str, ObservedReceipt] = {}
    for observed_receipt in receipts:
        if observed_receipt.transaction_hash in actual:
            raise ValueError("duplicate_wallet_receipt")
        actual[observed_receipt.transaction_hash] = observed_receipt
    per_tx: dict[str, list[Movement]] = defaultdict(list)
    unique = set()
    shapes: dict[str, tuple[int, str | None, int | None]] = {}
    index_hashes: dict[int, str] = {}
    index_positions: dict[tuple[int, int], str] = {}
    for row in index:
        if account not in (row.sender, row.recipient) or row.token not in opening:
            raise ValueError("wallet_index_outside_account_or_assets")
        identity = (row.transaction, row.kind, row.trace_id)
        # ERC20 index rows lack logIndex. Preserve multiplicity for receipt matching.
        if row.kind != "erc20":
            if identity in unique:
                raise ValueError("duplicate_wallet_index_transaction_or_trace")
            unique.add(identity)
        current = row.block, row.block_hash, row.transaction_index
        prior = shapes.setdefault(row.transaction, current)
        if prior[0] != current[0] or (prior[1] and current[1] and prior != current):
            raise ValueError("wallet_index_transaction_position_conflict")
        if current[1]:
            if index_hashes.setdefault(row.block, current[1]) != current[1]:
                raise ValueError("wallet_index_block_hash_conflict")
            if row.transaction_index is None:
                raise ValueError("wallet_index_transaction_position_missing")
            if (
                index_positions.setdefault((row.block, row.transaction_index), row.transaction)
                != row.transaction
            ):
                raise ValueError("wallet_index_transaction_position_conflict")
            shapes[row.transaction] = current
        per_tx[row.transaction].append(row)
    indexed_net: Counter[str | None] = Counter()
    checked_net: Counter[str | None] = Counter()
    checked_rows: Counter[str | None] = Counter()
    disagreements = []
    gas_indexed = gas_observed = gas_matched = 0
    observed = 0
    for tx, rows in sorted(per_tx.items()):
        receipt = actual.get(tx)
        observed += receipt is not None
        if receipt:
            block, block_hash, position = shapes[tx]
            if (
                receipt.block.number != block
                or (block_hash is not None and receipt.block.block_hash != block_hash)
                or (position is not None and receipt.transaction_index != position)
            ):
                raise ValueError("wallet_index_receipt_position_mismatch")
        claimed_transfers: Counter[tuple[str, str, str, int]] = Counter()
        for row in rows:
            if row.success:
                indexed_net[row.token] += row.delta(account)
            if row.kind == "erc20" and row.token:
                claimed_transfers[row.token, row.sender, row.recipient, row.value_raw] += 1
            if row.kind == "external" and row.sender == account:
                gas_indexed += row.gas_paid_raw
                if receipt and receipt.sender == account:
                    gas_actual = receipt.gas_used * receipt.effective_gas_price
                    gas_observed += gas_actual
                    gas_matched += gas_actual == row.gas_paid_raw
                    if gas_actual != row.gas_paid_raw:
                        disagreements.append({"transaction": tx, "reason": "gas_mismatch"})
            if (
                row.kind == "external"
                and receipt
                and (
                    row.success != bool(receipt.status)
                    or row.sender != receipt.sender
                    or row.recipient != receipt.recipient
                )
            ):
                disagreements.append(
                    {"transaction": tx, "reason": "external_status_or_roles_mismatch"}
                )
        if not receipt:
            continue
        seen_transfers = Counter(
            transfer_row
            for log in receipt.logs
            if log.address in opening
            and (transfer_row := transfer(log))
            and account in transfer_row[1:3]
        )
        for token in (asset for asset in opening if asset is not None):
            proposed = Counter({key: n for key, n in claimed_transfers.items() if key[0] == token})
            observed_transfers = Counter(
                {key: n for key, n in seen_transfers.items() if key[0] == token}
            )
            if proposed != observed_transfers:
                disagreements.append(
                    {"transaction": tx, "reason": "erc20_claim_multiset_mismatch", "token": token}
                )
                continue
            for (_, sender, recipient, amount), n in proposed.items():
                checked_net[token] += amount * n * ((recipient == account) - (sender == account))
                checked_rows[token] += n
    indexed_net[None] -= gas_indexed
    asset_rows = []
    for asset in sorted(opening, key=lambda key: key or ""):
        change = closing[asset] - opening[asset]
        asset_rows.append(
            {
                "asset": asset or "native",
                "opening_raw": opening[asset],
                "closing_raw": closing[asset],
                "indexed_net_raw": indexed_net[asset],
                "balance_minus_index_raw": change - indexed_net[asset],
                "receipt_matched_transfer_rows": checked_rows[asset],
                "receipt_matched_transfer_net_raw": checked_net[asset]
                if asset is not None
                else None,
            }
        )
    return {
        "assets": asset_rows,
        "indexed_transactions": len(per_tx),
        "receipts_found": observed,
        "transactions_without_receipts": sorted(per_tx.keys() - actual.keys()),
        "indexed_gas_paid_raw": gas_indexed,
        "receipt_gas_for_indexed_senders_raw": gas_observed,
        "gas_exact_matches": gas_matched,
        "disagreements": disagreements,
        "internal_traces_independently_verified": False,
        "complete_wallet_history_verified": False,
        "net_profit": None,
    }
