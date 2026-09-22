from dataclasses import dataclass
from enum import StrEnum

from ..models.common import BlockRef, EvidenceRef
from .inventory import PositionInventory, position_inventory
from .position_ledger import PositionLedgerEntry, PositionTokenLedger

Q192 = 1 << 192


class AccountingReplayStatus(StrEnum):
    COMPLETE = "complete"
    UNRESOLVED_SETTLEMENT = "unresolved_settlement"
    UNRESOLVED_OWNERSHIP = "unresolved_ownership"


@dataclass(frozen=True, slots=True)
class ValuationPoint:
    block: BlockRef
    sqrt_price_x96: int
    evidence: EvidenceRef

    def __post_init__(self) -> None:
        if (
            isinstance(self.sqrt_price_x96, bool)
            or not isinstance(self.sqrt_price_x96, int)
            or not 0 < self.sqrt_price_x96 < 1 << 160
        ):
            raise ValueError("valuation sqrt price must be a positive uint160")
        if self.evidence.block != self.block:
            raise ValueError("valuation evidence must be pinned to the valuation block")

    @property
    def price_x192(self) -> int:
        return self.sqrt_price_x96**2


@dataclass(frozen=True, slots=True)
class ValuedLedgerEntry:
    entry: PositionLedgerEntry
    valuation: ValuationPoint

    def __post_init__(self) -> None:
        if self.valuation.block != self.entry.block:
            raise ValueError("ledger entry valuation must use the entry block")


@dataclass(frozen=True, slots=True)
class AccountingFlow:
    position: tuple[int, int, int]
    transaction_hash: str
    amount0: int
    amount1: int
    price_x192: int
    quote1_value: int


@dataclass(frozen=True, slots=True)
class AccountingReplayInputs:
    opening: ValuationPoint
    closing: ValuationPoint
    entries: tuple[ValuedLedgerEntry, ...]
    external_cost_evidence: tuple[EvidenceRef, ...]
    beneficial_owner_continuity_evidence: EvidenceRef | None


@dataclass(frozen=True, slots=True)
class PositionAccountingReplay:
    status: AccountingReplayStatus
    ledger: PositionTokenLedger
    opening_inventory: PositionInventory
    closing_inventory: PositionInventory
    opening_value1: int
    closing_value1: int
    flows: tuple[AccountingFlow, ...]
    cumulative_flow_value1: int | None
    external_cost1: int
    net_pnl1: int | None
    inputs: AccountingReplayInputs


def value_in_currency1(amount0: int, amount1: int, price_x192: int) -> int:
    for name, value in (("amount0", amount0), ("amount1", amount1), ("price_x192", price_x192)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
    if price_x192 <= 0:
        raise ValueError("price_x192 must be positive")
    converted0 = abs(amount0) * price_x192 // Q192
    if amount0 < 0:
        converted0 = -converted0
    return converted0 + amount1


def replay_position_accounting(
    ledger: PositionTokenLedger,
    opening_valuation: ValuationPoint,
    closing_valuation: ValuationPoint,
    valued_entries: tuple[ValuedLedgerEntry, ...],
    *,
    external_cost1: int = 0,
    external_cost_evidence: tuple[EvidenceRef, ...] = (),
    beneficial_owner_continuity_evidence: EvidenceRef | None = None,
) -> PositionAccountingReplay:
    inputs = AccountingReplayInputs(
        opening_valuation,
        closing_valuation,
        tuple(valued_entries),
        tuple(external_cost_evidence),
        beneficial_owner_continuity_evidence,
    )
    if opening_valuation.block != ledger.opening.block:
        raise ValueError("opening valuation does not match opening position snapshot")
    if closing_valuation.block != ledger.closing.block:
        raise ValueError("closing valuation does not match closing position snapshot")
    if (
        isinstance(external_cost1, bool)
        or not isinstance(external_cost1, int)
        or external_cost1 < 0
    ):
        raise ValueError("external cost must be a nonnegative currency1 base-unit amount")
    if external_cost1 and not external_cost_evidence:
        raise ValueError("nonzero external cost requires evidence")
    for reference in external_cost_evidence:
        if reference.block is None or reference.block.chain_id != ledger.opening.block.chain_id:
            raise ValueError("external cost evidence must be block-pinned to the ledger chain")
    ownership = ledger.ownership_replay
    # Equal endpoint owners cannot rule out an intervening sale/transfer and return.
    owner_changed = ledger.opening.nft_owner != ledger.closing.nft_owner or (
        ownership is not None
        and any(event.from_owner != event.to_owner for event in ownership.transfers)
    )
    if beneficial_owner_continuity_evidence is not None:
        reference = beneficial_owner_continuity_evidence
        if ownership is None:
            raise ValueError("continuity evidence cannot replace a missing ownership replay")
        if not owner_changed:
            raise ValueError("unchanged NFT ownership does not require continuity evidence")
        if reference.kind != "beneficial_owner_continuity" or reference.block is None:
            raise ValueError(
                "ownership continuity requires block-pinned evidence of the right kind"
            )
        if (
            reference.block.chain_id != ledger.opening.block.chain_id
            or not ledger.opening.block.number
            < reference.block.number
            <= ledger.closing.block.number
        ):
            raise ValueError("ownership continuity evidence is outside the accounting interval")

    opening_inventory = position_inventory(ledger.opening, opening_valuation.sqrt_price_x96)
    closing_inventory = position_inventory(ledger.closing, closing_valuation.sqrt_price_x96)
    opening_value = value_in_currency1(
        opening_inventory.total0,
        opening_inventory.total1,
        opening_valuation.price_x192,
    )
    closing_value = value_in_currency1(
        closing_inventory.total0,
        closing_inventory.total1,
        closing_valuation.price_x192,
    )
    if not ledger.settlement_complete:
        if valued_entries:
            raise ValueError("unresolved ledger cannot contain selectively valued settlements")
        return PositionAccountingReplay(
            AccountingReplayStatus.UNRESOLVED_SETTLEMENT,
            ledger,
            opening_inventory,
            closing_inventory,
            opening_value,
            closing_value,
            (),
            None,
            external_cost1,
            None,
            inputs,
        )
    if ownership is None or (owner_changed and beneficial_owner_continuity_evidence is None):
        if valued_entries:
            raise ValueError("unresolved ownership cannot contain selectively valued settlements")
        return PositionAccountingReplay(
            AccountingReplayStatus.UNRESOLVED_OWNERSHIP,
            ledger,
            opening_inventory,
            closing_inventory,
            opening_value,
            closing_value,
            (),
            None,
            external_cost1,
            None,
            inputs,
        )

    if tuple(item.entry for item in valued_entries) != ledger.entries:
        raise ValueError("valued entries must exactly match the complete position ledger")
    flows: list[AccountingFlow] = []
    cumulative = 0
    for item in valued_entries:
        amount0 = item.entry.owner_amount0
        amount1 = item.entry.owner_amount1
        assert amount0 is not None
        assert amount1 is not None
        quote_value = value_in_currency1(amount0, amount1, item.valuation.price_x192)
        cumulative += quote_value
        flows.append(
            AccountingFlow(
                item.entry.position,
                item.entry.transaction_hash,
                amount0,
                amount1,
                item.valuation.price_x192,
                quote_value,
            )
        )
    return PositionAccountingReplay(
        AccountingReplayStatus.COMPLETE,
        ledger,
        opening_inventory,
        closing_inventory,
        opening_value,
        closing_value,
        tuple(flows),
        cumulative,
        external_cost1,
        closing_value + cumulative - opening_value - external_cost1,
        inputs,
    )


__all__ = [
    "AccountingFlow",
    "AccountingReplayStatus",
    "PositionAccountingReplay",
    "ValuationPoint",
    "ValuedLedgerEntry",
    "replay_position_accounting",
    "value_in_currency1",
]
