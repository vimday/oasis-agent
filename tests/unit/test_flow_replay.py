from __future__ import annotations

import unittest
from dataclasses import replace

from oasis_agent.evidence.ingestion import IngestionTrust, VerifiedBlockRange
from oasis_agent.farming import (
    FlowClassification,
    FlowEvidence,
    FlowEvidenceClaim,
    PoolFlowEconomicAttribution,
    ReplayPoolFlowObservation,
    build_pool_flow_from_replay,
    build_pool_flows_from_replay,
    derive_replay_flow_observations,
)
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef, EvidenceLevel, EvidenceRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.collector import BlockRangeDataset, require_provider_agreement
from oasis_agent.uniswap_v4.dataset_replay import decode_pool_dataset, replay_pool_dataset
from oasis_agent.uniswap_v4.events import POOL_INITIALIZED_TOPIC, SWAP_TOPIC
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


def word(value: int) -> str:
    return f"{value & ((1 << 256) - 1):064x}"


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class FlowReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = "0x" + "a" * 40
        self.h9 = "0x" + "9" * 64
        self.h10 = "0x" + "1" * 64
        self.h11 = "0x" + "2" * 64
        self.blocks = (
            BlockSnapshot(BlockRef(4663, 10, self.h10, 1_000), self.h9),
            BlockSnapshot(BlockRef(4663, 11, self.h11, 1_002), self.h10),
        )
        self.key = PoolKey(
            "0x" + "1" * 40,
            "0x" + "2" * 40,
            DYNAMIC_FEE_FLAG,
            60,
            "0x" + "3" * 40,
        )
        self.pool_id = pool_id_from_key(self.key)
        self.logs = (
            LogRecord(
                self.manager,
                (
                    POOL_INITIALIZED_TOPIC,
                    self.pool_id,
                    address_topic(self.key.currency0),
                    address_topic(self.key.currency1),
                ),
                "0x"
                + "".join(
                    (
                        word(self.key.fee),
                        word(self.key.tick_spacing),
                        word(int(self.key.hooks, 16)),
                        word(sqrt_price_at_tick(0)),
                        word(0),
                    )
                ),
                10,
                "0x" + "4" * 64,
                0,
                self.h10,
                0,
            ),
            LogRecord(
                self.manager,
                (SWAP_TOPIC, self.pool_id, address_topic("0x" + "4" * 40)),
                "0x"
                + "".join(
                    (
                        word(-100),
                        word(90),
                        word(sqrt_price_at_tick(1)),
                        word(500),
                        word(1),
                        word(2_000),
                    )
                ),
                11,
                "0x" + "6" * 64,
                0,
                self.h11,
                0,
            ),
        )
        self.dataset = BlockRangeDataset("provider-a", self.blocks, self.logs)
        self.verified = VerifiedBlockRange(
            self.dataset,
            EvidenceRef(
                "canonical_block_range",
                self.dataset.content_hash,
                self.blocks[-1].ref,
                "provider-a",
                "2026-09-05T00:00:00+00:00",
                request_hash="1" * 64,
                raw_response_hash="2" * 64,
                interpretation_hash="3" * 64,
                schema_version=2,
            ),
            IngestionTrust.SINGLE_PROVIDER_RESEARCH,
            None,
        )
        self.decoded = decode_pool_dataset(
            self.dataset, pool_manager=self.manager, pool_id=self.pool_id
        )
        self.replay = replay_pool_dataset(self.decoded, self.dataset)

    def observations(self) -> tuple[ReplayPoolFlowObservation, ...]:
        return derive_replay_flow_observations(
            self.verified,
            self.decoded,
            self.replay,
            currency0_decimals=18,
            currency1_decimals=6,
        )

    def evidence(self, claim: FlowEvidenceClaim, *, log_index: int | None = None) -> FlowEvidence:
        observation = self.observations()[0]
        subject_log_index = observation.log_index if log_index is None else log_index
        kind = {
            FlowEvidenceClaim.CLASSIFICATION: "trace",
            FlowEvidenceClaim.AMOUNTS: "receipt",
            FlowEvidenceClaim.COSTS: "price_markout",
        }[claim]
        reference = EvidenceRef(
            kind,
            "4" * 64,
            observation.block,
            "archive-a",
            "2026-09-05T00:00:00+00:00",
            request_hash="5" * 64,
            raw_response_hash="6" * 64,
            interpretation_hash="7" * 64,
        )
        return FlowEvidence(
            reference,
            observation.transaction_hash,
            observation.pool_id,
            subject_log_index,
            claim,
            subject_block=observation.block,
        )

    def attribution(self, **overrides: object) -> PoolFlowEconomicAttribution:
        observation = self.observations()[0]
        values: dict[str, object] = {
            "chain_id": observation.chain_id,
            "pool_id": observation.pool_id,
            "currency1": observation.currency1,
            "currency1_decimals": observation.currency1_decimals,
            "block": observation.block,
            "transaction_hash": observation.transaction_hash,
            "log_index": observation.log_index,
            "lp_fee1": 1,
            "gas_cost1": 2,
            "adverse_selection_cost1": 3,
            "self_trade_cost1": 0,
            "classification": FlowClassification.EXTERNAL,
            "evidence_level": EvidenceLevel.CONFIRMED,
            "evidence": (
                self.evidence(FlowEvidenceClaim.CLASSIFICATION),
                self.evidence(FlowEvidenceClaim.COSTS),
            ),
        }
        values.update(overrides)
        if "log_index" in overrides and "evidence" not in overrides:
            values["evidence"] = (
                self.evidence(FlowEvidenceClaim.CLASSIFICATION, log_index=values["log_index"]),
                self.evidence(FlowEvidenceClaim.COSTS, log_index=values["log_index"]),
            )
        return PoolFlowEconomicAttribution(**values)  # type: ignore[arg-type]

    def test_derives_replay_bound_observation_with_range_evidence(self) -> None:
        (observation,) = self.observations()

        self.assertEqual(observation.identity, (4663, self.pool_id, "0x" + "6" * 64, 0))
        self.assertEqual(observation.block, self.blocks[1].ref)
        self.assertEqual(observation.amount0, -100)
        self.assertEqual(observation.amount1, 90)
        self.assertEqual(observation.volume1, 90)
        self.assertEqual(observation.effective_fee, 2_000)
        self.assertEqual(observation.amount_evidence.reference, self.verified.evidence)
        self.assertEqual(observation.amount_evidence.subject_block, observation.block)
        self.assertEqual(observation.trust, IngestionTrust.SINGLE_PROVIDER_RESEARCH)

    def test_builds_confirmed_flow_only_after_explicit_enrichment(self) -> None:
        agreement = require_provider_agreement(
            (self.dataset, replace(self.dataset, source="provider-b"))
        )
        self.verified = replace(
            self.verified,
            trust=IngestionTrust.PROVIDER_QUORUM,
            agreement=agreement,
            peer_evidence=(replace(self.verified.evidence, source="provider-b"),),
        )
        observation = self.observations()[0]
        attribution = self.attribution()
        flow = build_pool_flow_from_replay(
            observation,
            replace(
                attribution,
                evidence=(*attribution.evidence, self.evidence(FlowEvidenceClaim.AMOUNTS)),
            ),
        )

        self.assertEqual(flow.volume1, 90)
        self.assertEqual(flow.lp_fee1, 1)
        self.assertEqual(flow.gas_cost1, 2)
        self.assertEqual(flow.adverse_selection_cost1, 3)
        self.assertEqual(flow.classification, FlowClassification.EXTERNAL)
        self.assertEqual(flow.evidence_level, EvidenceLevel.CONFIRMED)
        self.assertEqual(
            {item.claim for item in flow.evidence},
            {
                FlowEvidenceClaim.AMOUNTS,
                FlowEvidenceClaim.CLASSIFICATION,
                FlowEvidenceClaim.COSTS,
            },
        )

    def test_missing_explicit_economic_attribution_remains_unresolved(self) -> None:
        observation = self.observations()[0]
        attribution = self.attribution(
            classification=FlowClassification.UNKNOWN,
            evidence_level=EvidenceLevel.UNRESOLVED,
            lp_fee1=0,
            gas_cost1=0,
            adverse_selection_cost1=0,
            evidence=(),
        )
        flow = build_pool_flow_from_replay(observation, attribution)

        self.assertEqual(flow.classification, FlowClassification.UNKNOWN)
        self.assertEqual(flow.evidence_level, EvidenceLevel.UNRESOLVED)
        self.assertEqual(flow.evidence, (observation.amount_evidence,))

    def test_rejects_replay_identity_and_dataset_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "decoded pool inputs do not match"):
            derive_replay_flow_observations(
                self.verified,
                replace(self.decoded, pool_id="0x" + "9" * 32),
                self.replay,
                currency0_decimals=18,
                currency1_decimals=6,
            )
        with self.assertRaisesRegex(ValueError, "replay swap steps do not match"):
            derive_replay_flow_observations(
                self.verified,
                self.decoded,
                replace(self.replay, steps=()),
                currency0_decimals=18,
                currency1_decimals=6,
            )

    def test_rejects_mismatched_or_duplicate_enrichment(self) -> None:
        observation = self.observations()[0]
        with self.assertRaisesRegex(ValueError, "attribution identity does not match"):
            build_pool_flow_from_replay(
                observation,
                self.attribution(log_index=1),
            )
        with self.assertRaisesRegex(ValueError, "duplicate attribution identity"):
            build_pool_flows_from_replay(
                (observation,),
                (self.attribution(), self.attribution()),
            )

    def test_rejects_changed_amounts_fees_and_final_state_with_original_evidence(self) -> None:
        step = self.replay.steps[0]
        mutations = (
            replace(self.replay, steps=(replace(step, amount1=900_000),)),
            replace(self.replay, steps=(replace(step, fee=100_000),)),
            replace(self.replay, final_state=replace(self.replay.final_state, liquidity=999)),
        )
        for replay in mutations:
            with self.subTest(replay=replay), self.assertRaises(ValueError):
                derive_replay_flow_observations(
                    self.verified,
                    self.decoded,
                    replay,
                    currency0_decimals=18,
                    currency1_decimals=6,
                )

    def test_rejects_jointly_forged_decoded_event_and_replay(self) -> None:
        event = self.decoded.swaps[0]
        decoded = replace(
            self.decoded,
            swaps=(replace(event, observation=replace(event.observation, amount1=900_000)),),
        )
        # Merely copying the original dataset hash must not attest altered event values.
        with self.assertRaises(ValueError):
            replay = replay_pool_dataset(decoded, self.dataset)
            derive_replay_flow_observations(
                self.verified,
                decoded,
                replay,
                currency0_decimals=18,
                currency1_decimals=6,
            )

    def test_rejects_unproved_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmed flow requires"):
            build_pool_flow_from_replay(
                self.observations()[0],
                self.attribution(evidence=(self.evidence(FlowEvidenceClaim.CLASSIFICATION),)),
            )

    def test_single_provider_cannot_be_promoted_by_economic_enrichment(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider quorum"):
            build_pool_flow_from_replay(self.observations()[0], self.attribution())

    def test_swap_amount_evidence_does_not_prove_attributed_lp_fees(self) -> None:
        agreement = require_provider_agreement(
            (self.dataset, replace(self.dataset, source="provider-b"))
        )
        self.verified = replace(
            self.verified,
            trust=IngestionTrust.PROVIDER_QUORUM,
            agreement=agreement,
            peer_evidence=(replace(self.verified.evidence, source="provider-b"),),
        )
        with self.assertRaisesRegex(ValueError, "LP fee amount evidence"):
            build_pool_flow_from_replay(self.observations()[0], self.attribution())


if __name__ == "__main__":
    unittest.main()
