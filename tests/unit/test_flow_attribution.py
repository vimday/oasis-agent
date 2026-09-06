from __future__ import annotations

import unittest

from oasis_agent.farming import (
    FlowClassification,
    FlowEvidence,
    FlowEvidenceClaim,
    PoolFlow,
    PoolFlowForensicContext,
    analyze_pool_flows,
    build_pool_flow_forensic_report,
    render_pool_flow_forensic_markdown,
)
from oasis_agent.models.common import BlockRef, EvidenceLevel, EvidenceRef


class FlowAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.block = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        self.pool_id = "0x" + "55" * 32
        self.currency1 = "0x" + "77" * 20

    def evidence_for(
        self,
        transaction_hash: str,
        *,
        block: BlockRef | None = None,
        pool_id: str | None = None,
        log_index: int = 0,
        kind: str = "synthetic_flow_fixture",
        request_hash: str = "2" * 64,
        raw_response_hash: str = "3" * 64,
        interpretation_hash: str = "4" * 64,
        claims: tuple[FlowEvidenceClaim, ...] = (
            FlowEvidenceClaim.CLASSIFICATION,
            FlowEvidenceClaim.AMOUNTS,
            FlowEvidenceClaim.COSTS,
        ),
    ) -> tuple[FlowEvidence, ...]:
        reference = EvidenceRef(
            kind,
            "1" * 64,
            block or self.block,
            "archive-a",
            "2026-09-04T00:00:00+00:00",
            request_hash=request_hash,
            raw_response_hash=raw_response_hash,
            interpretation_hash=interpretation_hash,
            schema_version=2,
        )
        return tuple(
            FlowEvidence(
                reference=reference,
                transaction_hash=transaction_hash,
                pool_id=pool_id or self.pool_id,
                log_index=log_index,
                claim=claim,
            )
            for claim in claims
        )

    def flow(self, **overrides: object) -> PoolFlow:
        transaction_hash = str(overrides.get("transaction_hash", "0x" + "11" * 32))
        log_index = int(overrides.get("log_index", 0))
        values: dict[str, object] = {
            "chain_id": 4663,
            "pool_id": self.pool_id,
            "currency1": self.currency1,
            "currency1_decimals": 6,
            "block": self.block,
            "transaction_hash": transaction_hash,
            "log_index": log_index,
            "volume1": 10_000,
            "lp_fee1": 100,
            "gas_cost1": 3,
            "adverse_selection_cost1": 7,
            "evidence": self.evidence_for(transaction_hash, log_index=log_index),
            "evidence_level": EvidenceLevel.CONFIRMED,
            "classification": FlowClassification.EXTERNAL,
        }
        values.update(overrides)
        return PoolFlow(**values)  # type: ignore[arg-type]

    def context(self, **overrides: object) -> PoolFlowForensicContext:
        values: dict[str, object] = {
            "chain_id": 4663,
            "pool_id": self.pool_id,
            "hook": "0x" + "66" * 20,
            "currency1": "USDG",
            "currency1_address": self.currency1,
            "currency1_decimals": 6,
            "opening_block": BlockRef(4663, 9, "0x" + "99" * 32, 999),
            "closing_block": self.block,
            "report_status": "fixture_only",
        }
        values.update(overrides)
        return PoolFlowForensicContext(**values)  # type: ignore[arg-type]

    def test_affiliated_volume_is_not_external_alpha(self) -> None:
        affiliated = self.flow(
            classification=FlowClassification.AFFILIATED,
            self_trade_cost1=95,
            transaction_hash="0x" + "22" * 32,
            evidence=self.evidence_for("0x" + "22" * 32),
        )
        external = self.flow(
            classification=FlowClassification.EXTERNAL,
            transaction_hash="0x" + "33" * 32,
            log_index=1,
            evidence=self.evidence_for("0x" + "33" * 32, log_index=1),
        )
        result = analyze_pool_flows((affiliated, external))
        self.assertEqual(result.affiliated_volume1, 10_000)
        self.assertEqual(result.external_volume1, 10_000)
        self.assertEqual(result.external_fee_alpha1, 90)
        self.assertEqual(result.self_trade_cost1, 95)
        self.assertEqual(result.net_lp_pnl1, -15)

    def test_unresolved_external_flow_does_not_prove_alpha(self) -> None:
        unresolved = self.flow(
            classification=FlowClassification.EXTERNAL,
            evidence_level=EvidenceLevel.INFERRED,
            evidence=self.evidence_for(
                "0x" + "11" * 32,
                claims=(FlowEvidenceClaim.CLASSIFICATION,),
            ),
        )
        result = analyze_pool_flows((unresolved,))
        self.assertEqual(result.unresolved_flow_count, 1)
        self.assertEqual(result.external_fee_alpha1, 0)

    def test_confirmed_external_flow_requires_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit flow classification requires evidence"):
            self.flow(classification=FlowClassification.EXTERNAL, evidence=())

    def test_confirmed_flow_requires_amount_and_cost_evidence(self) -> None:
        cases = {
            "amounts": (FlowEvidenceClaim.CLASSIFICATION, FlowEvidenceClaim.COSTS),
            "costs": (FlowEvidenceClaim.CLASSIFICATION, FlowEvidenceClaim.AMOUNTS),
        }
        for missing_claim, claims in cases.items():
            with (
                self.subTest(missing_claim=missing_claim),
                self.assertRaisesRegex(ValueError, "confirmed flow requires"),
            ):
                self.flow(evidence=self.evidence_for("0x" + "11" * 32, claims=claims))

    def test_unknown_flow_cannot_be_confirmed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown flow cannot be confirmed"):
            self.flow(
                classification=FlowClassification.UNKNOWN,
                evidence=self.evidence_for(
                    "0x" + "11" * 32,
                    claims=(FlowEvidenceClaim.AMOUNTS, FlowEvidenceClaim.COSTS),
                ),
            )

    def test_non_affiliated_flow_rejects_self_trade_cost(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "self_trade_cost1 requires affiliated classification"
        ):
            self.flow(classification=FlowClassification.UNKNOWN, self_trade_cost1=1)

    def test_inferred_affiliated_flow_remains_unresolved(self) -> None:
        result = analyze_pool_flows(
            (
                self.flow(
                    classification=FlowClassification.AFFILIATED,
                    evidence_level=EvidenceLevel.INFERRED,
                    self_trade_cost1=95,
                    evidence=self.evidence_for(
                        "0x" + "11" * 32,
                        claims=(FlowEvidenceClaim.CLASSIFICATION,),
                    ),
                ),
            )
        )
        self.assertEqual(result.unresolved_flow_count, 1)
        self.assertEqual(result.affiliated_lp_fee1, 100)
        self.assertEqual(result.external_fee_alpha1, 0)

    def test_external_fee_alpha_preserves_losses(self) -> None:
        result = analyze_pool_flows(
            (
                self.flow(
                    lp_fee1=100,
                    gas_cost1=80,
                    adverse_selection_cost1=30,
                ),
            )
        )
        self.assertEqual(result.external_fee_alpha1, -10)
        self.assertEqual(result.net_lp_pnl1, -10)

    def test_affiliated_flow_requires_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.flow(classification=FlowClassification.AFFILIATED, evidence=())

    def test_evidence_is_deeply_immutable(self) -> None:
        references = list(self.evidence_for("0x" + "11" * 32))
        flow = self.flow(classification=FlowClassification.EXTERNAL, evidence=references)
        references.clear()
        self.assertEqual(len(flow.evidence), 3)

    def test_classification_evidence_requires_complete_provenance(self) -> None:
        for field in ("request_hash", "raw_response_hash", "interpretation_hash"):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    ValueError, "classification evidence requires complete provenance"
                ),
            ):
                self.flow(
                    classification=FlowClassification.EXTERNAL,
                    evidence=self.evidence_for(
                        "0x" + "11" * 32,
                        **{field: ""},
                    ),
                )

    def test_classification_evidence_must_match_flow_subject(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence transaction does not match flow"):
            self.flow(
                classification=FlowClassification.EXTERNAL,
                evidence=self.evidence_for("0x" + "22" * 32),
            )
        with self.assertRaisesRegex(ValueError, "evidence pool does not match flow"):
            self.flow(
                classification=FlowClassification.EXTERNAL,
                evidence=self.evidence_for("0x" + "11" * 32, pool_id="0x" + "88" * 32),
            )

    def test_unrelated_evidence_kind_cannot_prove_classification(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported classification evidence kind"):
            self.flow(
                classification=FlowClassification.EXTERNAL,
                evidence=self.evidence_for("0x" + "11" * 32, kind="token_metadata"),
            )

    def test_analyzer_rejects_mixed_asset_or_scope(self) -> None:
        base = self.flow()
        other_chain_block = BlockRef(1, 10, "0x" + "aa" * 32, 1_000)
        other_pool = "0x" + "88" * 32
        cases = {
            "chain": self.flow(
                chain_id=1,
                block=other_chain_block,
                evidence=self.evidence_for(
                    "0x" + "11" * 32,
                    block=other_chain_block,
                ),
            ),
            "pool": self.flow(
                pool_id=other_pool,
                evidence=self.evidence_for("0x" + "11" * 32, pool_id=other_pool),
            ),
            "token": self.flow(currency1="0x" + "99" * 20),
            "decimals": self.flow(currency1_decimals=18),
        }
        for name, other in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "flow scope mismatch"):
                analyze_pool_flows((base, other))

    def test_duplicate_flow_identity_is_rejected(self) -> None:
        flow = self.flow(classification=FlowClassification.EXTERNAL)
        with self.assertRaisesRegex(ValueError, "duplicate flow identity"):
            analyze_pool_flows((flow, flow))

    def test_same_transaction_different_log_indices_are_distinct(self) -> None:
        transaction_hash = "0x" + "11" * 32
        first = self.flow(transaction_hash=transaction_hash, log_index=0)
        second = self.flow(
            transaction_hash=transaction_hash,
            log_index=1,
            evidence=self.evidence_for(transaction_hash, log_index=1),
        )
        self.assertEqual(analyze_pool_flows((first, second)).flow_count, 2)

    def test_forensic_report_preserves_evidence_and_separates_fee_sources(self) -> None:
        affiliated = self.flow(
            classification=FlowClassification.AFFILIATED,
            self_trade_cost1=95,
            transaction_hash="0x" + "22" * 32,
            evidence=self.evidence_for("0x" + "22" * 32),
        )
        external = self.flow(
            classification=FlowClassification.EXTERNAL,
            transaction_hash="0x" + "33" * 32,
            log_index=1,
            evidence=self.evidence_for("0x" + "33" * 32, log_index=1),
        )
        unknown = self.flow(
            classification=FlowClassification.UNKNOWN,
            evidence_level=EvidenceLevel.UNRESOLVED,
            transaction_hash="0x" + "44" * 32,
            log_index=2,
            evidence=self.evidence_for("0x" + "44" * 32, log_index=2),
        )
        context = self.context()

        report = build_pool_flow_forensic_report(context, (affiliated, external, unknown))

        self.assertEqual(report.analysis.gross_lp_fee1, 300)
        self.assertEqual(report.analysis.external_fee_alpha1, 90)
        self.assertEqual(report.analysis.affiliated_lp_fee1, 100)
        self.assertEqual(report.analysis.unknown_lp_fee1, 100)
        self.assertEqual(report.analysis.net_lp_pnl1, -25)
        self.assertEqual(report.analysis.unresolved_flow_count, 1)
        self.assertEqual(report.analysis.confirmed_flow_count, 2)
        self.assertFalse(report.analysis.all_flows_economically_evidenced)
        self.assertEqual(len(report.evidence), 9)
        self.assertTrue(all(item.block == context.closing_block for item in report.evidence))
        self.assertTrue(all(item.request_hash == "2" * 64 for item in report.evidence))
        self.assertTrue(all(item.raw_response_hash == "3" * 64 for item in report.evidence))
        self.assertTrue(all(item.interpretation_hash == "4" * 64 for item in report.evidence))
        self.assertTrue(
            all(item.retrieved_at == "2026-09-04T00:00:00+00:00" for item in report.evidence)
        )
        self.assertTrue(all(item.schema_version == 2 for item in report.evidence))
        self.assertEqual(
            {claim.level for claim in report.claims},
            {EvidenceLevel.CONFIRMED, EvidenceLevel.INFERRED, EvidenceLevel.UNRESOLVED},
        )

        text = render_pool_flow_forensic_markdown(report)
        self.assertNotIn(
            "Every modeled flow classification, amount, and cost is bound to block-pinned evidence.",
            text,
        )
        self.assertIn(
            "Every modeled flow amount and cost is bound to block-pinned evidence; unknown classifications remain unresolved.",
            text,
        )
        self.assertIn("Fixture-only", text)
        self.assertIn("Currency1 token: `0x7777777777777777777777777777777777777777`", text)
        self.assertIn("Currency1 decimals: `6`", text)
        self.assertIn("Gross LP fees: `300 USDG base units`", text)
        self.assertIn("Confirmed external fee alpha: `90 USDG base units`", text)
        self.assertIn("Affiliated/self-classified LP fees: `100 USDG base units`", text)
        self.assertIn("Unknown-flow LP fees: `100 USDG base units`", text)
        self.assertIn("Self-trade cost: `95 USDG base units`", text)
        self.assertIn("archive-a", text)
        self.assertIn("Retrieved at: `2026-09-04T00:00:00+00:00`", text)
        self.assertIn("Block: `10`", text)
        self.assertIn("**confirmed**", text)
        self.assertIn("**inferred**", text)
        self.assertIn("**unresolved**", text)
        self.assertNotIn("APR", text)

    def test_report_rejects_flow_scope_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "flow scope does not match report context"):
            build_pool_flow_forensic_report(
                self.context(),
                (self.flow(currency1="0x" + "99" * 20),),
            )

    def test_report_requires_exact_snapshot_block_hash(self) -> None:
        conflicting = BlockRef(4663, 10, "0x" + "bb" * 32, 1_000)
        with self.assertRaisesRegex(ValueError, "flow block must match closing block"):
            build_pool_flow_forensic_report(
                self.context(),
                (
                    self.flow(
                        block=conflicting,
                        evidence=self.evidence_for("0x" + "11" * 32, block=conflicting),
                    ),
                ),
            )

    def test_report_rejects_earlier_flow_without_verified_lineage(self) -> None:
        closing = BlockRef(4663, 11, "0x" + "bb" * 32, 1_001)
        with self.assertRaisesRegex(ValueError, "flow block must match closing block"):
            build_pool_flow_forensic_report(
                self.context(closing_block=closing),
                (self.flow(),),
            )

    def test_report_does_not_call_unpinned_input_block_pinned(self) -> None:
        report = build_pool_flow_forensic_report(
            self.context(),
            (
                self.flow(
                    classification=FlowClassification.UNKNOWN,
                    evidence_level=EvidenceLevel.UNRESOLVED,
                    evidence=(),
                ),
            ),
        )
        text = render_pool_flow_forensic_markdown(report)
        self.assertIn("deterministic integer arithmetic over the supplied fixture inputs", text)
        self.assertIn("Some modeled amounts lack block-pinned evidence", text)
        self.assertNotIn("totals are deterministic arithmetic over the supplied block-pinned", text)
        self.assertIn("No block-pinned flow evidence was supplied", text)

    def test_forensic_context_rejects_cross_chain_snapshot(self) -> None:
        with self.assertRaisesRegex(ValueError, "context chain_id must match range blocks"):
            self.context(closing_block=BlockRef(1, 10, "0x" + "aa" * 32, 1_000))

    def test_rejects_invalid_transaction_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "transaction hash"):
            self.flow(transaction_hash="0x1234")


if __name__ == "__main__":
    unittest.main()
