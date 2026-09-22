"""Explicit cost coverage layered over marked NAV, never over fee-only APR.

IL, LVR, hook/protocol deductions and swap slippage already reflected in NAV and
owner cash flows are disclosures, not a second debit. Evidence here is required
but remains research evidence; this module cannot attest independent pricing.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, normalize_address
from .accounting_replay import PositionAccountingReplay, replay_position_accounting
from .position_ledger import build_position_token_ledger


class CostCategory(StrEnum):
    GAS = "gas"
    FAILED_EXECUTION = "failed_execution"
    REBALANCE = "rebalance"
    HEDGE = "hedge"
    FINANCING = "financing"
    OPPORTUNITY = "opportunity"
    PROTOCOL_HOOK = "protocol_hook"
    IL = "impermanent_loss"
    LVR = "lvr"
    INVENTORY_RISK = "inventory_risk"


class CostTreatment(StrEnum):
    EXTRA = "additional_debit"
    EMBEDDED = "already_in_nav_or_cashflow"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class CostDisclosure:
    category: CostCategory
    treatment: CostTreatment
    quote_token: str
    quote_decimals: int
    amount: int
    evidence: tuple[EvidenceRef, ...]
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, CostCategory) or not isinstance(
            self.treatment, CostTreatment
        ):
            raise TypeError("typed cost category and treatment required")
        object.__setattr__(self, "quote_token", normalize_address(self.quote_token))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if type(self.amount) is not int or self.amount < 0:
            raise ValueError("cost must be nonnegative integer quote base units")
        if type(self.quote_decimals) is not int or not 0 <= self.quote_decimals <= 255:
            raise ValueError("cost decimals must fit uint8")
        if not self.evidence or not self.rationale.strip():
            raise ValueError("even zero/N-A costs require evidence and rationale")
        if self.treatment is CostTreatment.NOT_APPLICABLE and self.amount != 0:
            raise ValueError("N-A cost must be zero")
        if (
            self.category in {CostCategory.IL, CostCategory.LVR, CostCategory.PROTOCOL_HOOK}
            and self.treatment is CostTreatment.EXTRA
        ):
            raise ValueError("NAV-embedded losses/fees must not be deducted twice")


@dataclass(frozen=True, slots=True)
class FullCostResult:
    quote_token: str
    quote_decimals: int
    missing: tuple[str, ...]
    additional_cost: int | None
    net_pnl: int | None
    execution_eligible: bool = field(default=False, init=False)


def reconcile_full_cost(
    replay: PositionAccountingReplay,
    disclosures: tuple[CostDisclosure, ...],
    *,
    quote_decimals: int,
    canonical_blocks: tuple[BlockSnapshot, ...],
) -> FullCostResult:
    if type(quote_decimals) is not int or not 0 <= quote_decimals <= 255:
        raise ValueError("quote decimals must fit uint8")
    # Avoid deducting an opaque legacy external-cost aggregate a second time.
    if replay.external_cost1 != 0:
        raise ValueError("supply pre-external-cost NAV replay and itemized costs")
    ledger = replay.ledger
    rebuilt = build_position_token_ledger(
        ledger.opening,
        ledger.closing,
        ledger.entries,
        canonical_blocks=canonical_blocks,
        ownership_replay=ledger.ownership_replay,
    )
    if rebuilt != ledger:
        raise ValueError("accounting ledger changed during cost reconciliation")
    inputs = replay.inputs
    recomputed = replay_position_accounting(
        rebuilt,
        inputs.opening,
        inputs.closing,
        inputs.entries,
        external_cost1=replay.external_cost1,
        external_cost_evidence=inputs.external_cost_evidence,
        beneficial_owner_continuity_evidence=inputs.beneficial_owner_continuity_evidence,
    )
    if recomputed != replay:
        raise ValueError("accounting replay differs from retained valuation inputs")
    blocks = {item.ref.number: item.ref for item in canonical_blocks}
    opening, closing = replay.ledger.opening.block, replay.ledger.closing.block
    token = replay.ledger.opening.pool_key.currency1
    seen: set[CostCategory] = set()
    hashes: set[str] = set()
    total = 0
    for item in disclosures:
        if item.category in seen:
            raise ValueError("duplicate cost category; aggregate evidence explicitly")
        seen.add(item.category)
        if (item.quote_token, item.quote_decimals) != (token, quote_decimals):
            raise ValueError("cost denomination mismatch")
        for ref in item.evidence:
            if (
                ref.block is None
                or ref.block.chain_id != opening.chain_id
                or not opening.number <= ref.block.number <= closing.number
                or blocks.get(ref.block.number) != ref.block
                or not ref.request_hash
                or not ref.raw_response_hash
                or not ref.interpretation_hash
            ):
                raise ValueError("cost evidence needs complete provenance within accounting window")
            if item.treatment is CostTreatment.EXTRA:
                if ref.content_hash in hashes:
                    raise ValueError("same cost evidence debited more than once")
                hashes.add(ref.content_hash)
        if item.treatment is CostTreatment.EXTRA:
            total += item.amount
    missing = tuple(sorted(x.value for x in CostCategory if x not in seen))
    if replay.net_pnl1 is None:
        missing += ("owner_settlement_or_valuation",)
    complete = not missing
    return FullCostResult(
        token,
        quote_decimals,
        missing,
        total if complete else None,
        replay.net_pnl1 - total if complete and replay.net_pnl1 is not None else None,
    )
