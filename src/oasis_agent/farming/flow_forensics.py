from dataclasses import dataclass

from ..models.common import (
    BlockRef,
    EvidenceLevel,
    normalize_address,
    normalize_hash32,
)
from .flow_attribution import (
    FlowEvidenceClaim,
    PoolFlow,
    PoolFlowAnalysis,
    analyze_pool_flows,
)


@dataclass(frozen=True, slots=True)
class PoolFlowForensicContext:
    """Identity and evidence boundary for one pool-flow analysis."""

    chain_id: int
    pool_id: str
    hook: str
    currency1: str
    currency1_address: str
    currency1_decimals: int
    opening_block: BlockRef
    closing_block: BlockRef
    report_status: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.chain_id, bool)
            or not isinstance(self.chain_id, int)
            or self.chain_id <= 0
        ):
            raise ValueError("chain_id must be a positive integer")
        if (
            self.chain_id != self.opening_block.chain_id
            or self.chain_id != self.closing_block.chain_id
        ):
            raise ValueError("context chain_id must match range blocks")
        if self.opening_block.number >= self.closing_block.number:
            raise ValueError("opening block must precede closing block")
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        object.__setattr__(self, "hook", normalize_address(self.hook))
        if not isinstance(self.currency1, str) or not self.currency1.strip():
            raise ValueError("currency1 is required")
        object.__setattr__(self, "currency1", self.currency1.strip())
        object.__setattr__(self, "currency1_address", normalize_address(self.currency1_address))
        if (
            isinstance(self.currency1_decimals, bool)
            or not isinstance(self.currency1_decimals, int)
            or not 0 <= self.currency1_decimals <= 255
        ):
            raise ValueError("currency1_decimals must be an integer from 0 through 255")
        if self.report_status != "fixture_only":
            raise ValueError("flow forensic reports currently require fixture_only status")

    @property
    def flow_scope(self) -> tuple[int, str, str, int]:
        return (
            self.chain_id,
            self.pool_id,
            self.currency1_address,
            self.currency1_decimals,
        )


@dataclass(frozen=True, slots=True)
class PoolFlowEvidence:
    transaction_hash: str
    pool_id: str
    log_index: int
    claim: FlowEvidenceClaim
    kind: str
    content_hash: str
    source: str
    block: BlockRef
    evidence_level: EvidenceLevel
    retrieved_at: str
    request_hash: str
    raw_response_hash: str
    interpretation_hash: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class PoolFlowClaim:
    level: EvidenceLevel
    text: str


@dataclass(frozen=True, slots=True)
class PoolFlowForensicReport:
    context: PoolFlowForensicContext
    analysis: PoolFlowAnalysis
    evidence: tuple[PoolFlowEvidence, ...]
    claims: tuple[PoolFlowClaim, ...]


def build_pool_flow_forensic_report(
    context: PoolFlowForensicContext, flows: tuple[PoolFlow, ...]
) -> PoolFlowForensicReport:
    """Build a conservative fixture report without inferring wallet ownership."""

    normalized = tuple(flows)
    for flow in normalized:
        if flow.scope != context.flow_scope:
            raise ValueError("flow scope does not match report context")
        if flow.block != context.closing_block:
            raise ValueError("flow block must match closing block")

    evidence = tuple(
        PoolFlowEvidence(
            transaction_hash=flow.transaction_hash,
            pool_id=item.pool_id,
            log_index=item.log_index,
            claim=item.claim,
            kind=item.reference.kind,
            content_hash=item.reference.content_hash,
            source=item.reference.source,
            block=item.reference.block,
            evidence_level=flow.evidence_level,
            retrieved_at=item.reference.retrieved_at,
            request_hash=item.reference.request_hash,
            raw_response_hash=item.reference.raw_response_hash,
            interpretation_hash=item.reference.interpretation_hash,
            schema_version=item.reference.schema_version,
        )
        for flow in normalized
        for item in flow.evidence
        if item.reference.block is not None
    )

    analysis = analyze_pool_flows(normalized)
    all_inputs_pinned = bool(normalized) and all(
        flow.has_complete_economic_evidence for flow in normalized
    )
    all_flows_confirmed = all_inputs_pinned and all(
        flow.evidence_level is EvidenceLevel.CONFIRMED for flow in normalized
    )
    claims = [
        PoolFlowClaim(
            EvidenceLevel.CONFIRMED,
            "The displayed totals are deterministic integer arithmetic over "
            "the supplied fixture inputs.",
        ),
        PoolFlowClaim(
            EvidenceLevel.INFERRED,
            "Affiliated labels are evidence-backed inputs, not independent "
            "proof of common beneficial ownership.",
        ),
        PoolFlowClaim(
            EvidenceLevel.UNRESOLVED,
            "Unknown or non-confirmed external flow does not establish "
            "unrelated market demand or fee alpha.",
        ),
    ]
    if all_flows_confirmed:
        claims.append(
            PoolFlowClaim(
                EvidenceLevel.CONFIRMED,
                "Every modeled flow classification, amount, and cost is "
                "bound to block-pinned evidence.",
            )
        )
    elif all_inputs_pinned:
        claims.append(
            PoolFlowClaim(
                EvidenceLevel.CONFIRMED,
                "Every modeled flow amount and cost is bound to block-pinned "
                "evidence; unknown classifications remain unresolved.",
            )
        )
    else:
        claims.append(
            PoolFlowClaim(
                EvidenceLevel.UNRESOLVED,
                "Some modeled amounts lack block-pinned evidence and remain "
                "unresolved fixture inputs.",
            )
        )
    return PoolFlowForensicReport(context, analysis, evidence, tuple(claims))


def render_pool_flow_forensic_markdown(
    report: PoolFlowForensicReport, *, title: str = "# Pool Flow Forensic Report"
) -> str:
    """Render token-base-unit economics and evidence without a headline yield metric."""

    context = report.context
    analysis = report.analysis
    unit = f"{context.currency1} base units"
    lines = [
        title,
        "",
        (
            "> **Fixture-only research report.** It is not live-chain evidence, "
            "a backtest, or a production decision."
        ),
        "",
        "## Snapshot identity",
        "",
        f"- Status: `{context.report_status}`",
        f"- Chain ID: `{context.chain_id}`",
        f"- Pool ID: `{context.pool_id}`",
        f"- Hook: `{context.hook}`",
        f"- Currency1 token: `{context.currency1_address}`",
        f"- Currency1 decimals: `{context.currency1_decimals}`",
        f"- Opening block (exclusive): `{context.opening_block.number}`",
        f"- Opening block hash: `{context.opening_block.block_hash}`",
        f"- Closing block (inclusive): `{context.closing_block.number}`",
        f"- Closing block hash: `{context.closing_block.block_hash}`",
        "",
        "## Conservative economic decomposition",
        "",
        f"- Flow count: `{analysis.flow_count}`",
        f"- Gross volume: `{analysis.volume1} {unit}`",
        f"- Gross LP fees: `{analysis.gross_lp_fee1} {unit}`",
        f"- Confirmed external fee alpha: `{analysis.external_fee_alpha1} {unit}`",
        f"- Affiliated/self-classified LP fees: `{analysis.affiliated_lp_fee1} {unit}`",
        f"- Unknown-flow LP fees: `{analysis.unknown_lp_fee1} {unit}`",
        f"- Self-trade cost: `{analysis.self_trade_cost1} {unit}`",
        f"- Gas cost: `{analysis.gas_cost1} {unit}`",
        f"- Adverse-selection cost: `{analysis.adverse_selection_cost1} {unit}`",
        f"- Net modeled LP PnL: `{analysis.net_lp_pnl1} {unit}`",
        f"- Unresolved flow count: `{analysis.unresolved_flow_count}`",
        f"- Confirmed flow count: `{analysis.confirmed_flow_count}`",
        (
            f"- All flows economically evidenced: "
            f"`{str(analysis.all_flows_economically_evidenced).lower()}`"
        ),
        "",
        "## Evidence-qualified claims",
        "",
    ]
    lines.extend(f"- **{claim.level.value}** — {claim.text}" for claim in report.claims)
    lines += ["", "## Flow evidence", ""]
    if report.evidence:
        for item in report.evidence:
            lines.extend(
                [
                    f"- Transaction: `{item.transaction_hash}`",
                    f"  - Pool ID: `{item.pool_id}`",
                    f"  - Log index: `{item.log_index}`",
                    f"  - Claim: `{item.claim.value}`",
                    f"  - Level: `{item.evidence_level.value}`",
                    f"  - Kind: `{item.kind}`",
                    f"  - Source: `{item.source}`",
                    f"  - Retrieved at: `{item.retrieved_at}`",
                    f"  - Block: `{item.block.number}`",
                    f"  - Block hash: `{item.block.block_hash}`",
                    f"  - Content hash: `{item.content_hash}`",
                    f"  - Request hash: `{item.request_hash}`",
                    f"  - Raw response hash: `{item.raw_response_hash}`",
                    f"  - Interpretation hash: `{item.interpretation_hash}`",
                    f"  - Schema version: `{item.schema_version}`",
                ]
            )
    else:
        lines.append("- No block-pinned flow evidence was supplied.")
    lines += [
        "",
        "## Safety interpretation",
        "",
        "- Gross fees are not treated as proof of unrelated external demand.",
        (
            "- Self-classified flow remains economically charged for modeled "
            "trading, gas, and adverse-selection costs."
        ),
        "- Unknown flow remains unresolved and cannot support a profitability or deployment claim.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "PoolFlowClaim",
    "PoolFlowEvidence",
    "PoolFlowForensicContext",
    "PoolFlowForensicReport",
    "build_pool_flow_forensic_report",
    "render_pool_flow_forensic_markdown",
]
