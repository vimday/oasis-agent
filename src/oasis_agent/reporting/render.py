import json
from pathlib import Path
from typing import Any

from ..farming.flow_attribution import (
    FlowClassification,
    FlowEvidence,
    FlowEvidenceClaim,
    PoolFlow,
)
from ..farming.flow_forensics import (
    PoolFlowForensicContext,
    build_pool_flow_forensic_report,
    render_pool_flow_forensic_markdown,
)
from ..models.common import BlockRef, EvidenceLevel, EvidenceRef
from ..models.v4 import DYNAMIC_FEE_FLAG


def _fixture_flow_report() -> str:
    opening_block = BlockRef(
        4663,
        53_576_330,
        "0xb439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b",
    )
    closing_block = BlockRef(
        4663,
        53_576_331,
        "0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b",
    )
    pool_id = "0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5"
    currency1 = "0x0000000000000000000000000000000000000001"

    def evidence(
        transaction_hash: str, digest: str, *, classified: bool
    ) -> tuple[FlowEvidence, ...]:
        reference = EvidenceRef(
            "synthetic_flow_fixture",
            digest * 64,
            closing_block,
            "synthetic-fixture",
            "2026-09-04T00:00:00+00:00",
            request_hash="4" * 64,
            raw_response_hash="5" * 64,
            interpretation_hash="6" * 64,
            schema_version=2,
        )
        claims = [FlowEvidenceClaim.AMOUNTS, FlowEvidenceClaim.COSTS]
        if classified:
            claims.append(FlowEvidenceClaim.CLASSIFICATION)
        return tuple(
            FlowEvidence(reference, transaction_hash, pool_id, 0, claim) for claim in claims
        )

    flows = (
        PoolFlow(
            chain_id=4663,
            pool_id=pool_id,
            currency1=currency1,
            currency1_decimals=6,
            block=closing_block,
            transaction_hash="0x" + "11" * 32,
            log_index=0,
            volume1=10_000,
            lp_fee1=100,
            gas_cost1=3,
            adverse_selection_cost1=7,
            self_trade_cost1=95,
            classification=FlowClassification.AFFILIATED,
            evidence_level=EvidenceLevel.CONFIRMED,
            evidence=evidence("0x" + "11" * 32, "1", classified=True),
        ),
        PoolFlow(
            chain_id=4663,
            pool_id=pool_id,
            currency1=currency1,
            currency1_decimals=6,
            block=closing_block,
            transaction_hash="0x" + "22" * 32,
            log_index=0,
            volume1=10_000,
            lp_fee1=100,
            gas_cost1=3,
            adverse_selection_cost1=7,
            classification=FlowClassification.EXTERNAL,
            evidence_level=EvidenceLevel.CONFIRMED,
            evidence=evidence("0x" + "22" * 32, "2", classified=True),
        ),
        PoolFlow(
            chain_id=4663,
            pool_id=pool_id,
            currency1=currency1,
            currency1_decimals=6,
            block=closing_block,
            transaction_hash="0x" + "33" * 32,
            log_index=0,
            volume1=10_000,
            lp_fee1=100,
            gas_cost1=3,
            adverse_selection_cost1=7,
            classification=FlowClassification.UNKNOWN,
            evidence_level=EvidenceLevel.UNRESOLVED,
            evidence=evidence("0x" + "33" * 32, "3", classified=False),
        ),
    )
    context = PoolFlowForensicContext(
        chain_id=4663,
        pool_id=pool_id,
        hook="0x1191Ac2561817686EFE4296B37BA5a5419Eaa880",
        currency1="USDG",
        currency1_address=currency1,
        currency1_decimals=6,
        opening_block=opening_block,
        closing_block=closing_block,
        report_status="fixture_only",
    )
    report = build_pool_flow_forensic_report(context, flows)
    return render_pool_flow_forensic_markdown(report, title="## Fixture flow-attribution example")


def benchmark_payload() -> dict[str, Any]:
    hook = "0x1191Ac2561817686EFE4296B37BA5a5419Eaa880"
    return {
        "report_status": "fixture_only",
        "data_provenance": "deterministic fixture; not live-chain evidence or a backtest",
        "title": "Robinhood Chain Uni v4 Benchmark Hook Analysis",
        "chain_id": 4663,
        "pool_id": "0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5",
        "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "hook": hook,
        "pool_fee": DYNAMIC_FEE_FLAG,
        "tick_spacing": 60,
        "permissions": {
            "before_initialize": True,
            "before_add_liquidity": True,
            "before_swap": True,
            "after_swap": False,
            "before_swap_returns_delta": False,
            "after_swap_returns_delta": False,
        },
        "default_fee_config": [10000, 20000, 30000, 200, 3400, 3300, 3300, 16777216],
        "findings": [
            {
                "level": "confirmed",
                "text": "`beforeSwap` is enabled by the hook address permissions.",
            },
            {
                "level": "confirmed",
                "text": "No swap-return-delta permission is enabled by the address mask.",
            },
            {
                "level": "inferred",
                "text": "Bytecode contains a fee override path and likely "
                "dynamic fee configuration.",
            },
            {
                "level": "unresolved",
                "text": "The exact fee-selection algorithm requires pinned "
                "fork execution or verified source.",
            },
        ],
        "limitations": [
            "Source code is not verified.",
            "This report is generated from deterministic local fixture data.",
            "No mainnet transaction is sent by the report command.",
            "Dynamic fee claims require block-pinned settlement reconciliation.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    pool_fee = int(payload["pool_fee"])
    fee_label = "dynamic-fee sentinel" if pool_fee == DYNAMIC_FEE_FLAG else "static fee pips"
    lines = [
        f"# {payload['title']}",
        "",
        (
            "> **Fixture-only research report.** This is not live-chain evidence, "
            "a backtest, or a production-readiness decision."
        ),
        "",
        "## Provenance",
        "",
        f"- Status: `{payload['report_status']}`",
        f"- Data: {payload['data_provenance']}",
        "",
        "## Identity",
        "",
        f"- Chain ID: `{payload['chain_id']}`",
        f"- Pool ID: `{payload['pool_id']}`",
        f"- PoolManager: `{payload['pool_manager']}`",
        f"- Hook: `{payload['hook']}`",
        f"- Pool fee field: `{pool_fee}` (`0x{pool_fee:06x}`, {fee_label})",
        f"- Tick spacing: `{payload['tick_spacing']}`",
        "",
        "## Hook permissions",
        "",
    ]
    lines.extend(
        f"- `{name}`: {'enabled' if enabled else 'disabled'}"
        for name, enabled in payload["permissions"].items()
    )
    lines += [
        "",
        "## Default fee configuration",
        "",
        "```text",
        json.dumps(payload["default_fee_config"]),
        "```",
        "",
        "## Findings",
        "",
    ]
    lines.extend(f"- **{finding['level']}** — {finding['text']}" for finding in payload["findings"])
    lines += ["", _fixture_flow_report(), "", "## Limitations and safety", ""]
    lines.extend(f"- {item}" for item in payload["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(benchmark_payload()), encoding="utf-8")
