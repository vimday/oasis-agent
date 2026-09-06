from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from oasis_agent.evidence.chain_store import CanonicalChainStore
from oasis_agent.evidence.ingestion import (
    IngestionTrust,
    apply_verified_block_range,
    persist_and_verify_block_range,
)
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.collector import BlockRangeDataset, ProviderAgreement
from tests.rpc_evidence_helpers import range_exchanges


class VerifiedIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        block = BlockRef(4663, 10, "0x" + "a" * 64, 1_000)
        self.dataset = BlockRangeDataset(
            "provider-a",
            (BlockSnapshot(block, "0x" + "9" * 64),),
            (
                LogRecord(
                    "0x" + "1" * 40,
                    ("0x" + "2" * 64,),
                    "0x",
                    10,
                    "0x" + "3" * 64,
                    0,
                    block.block_hash,
                    0,
                ),
            ),
        )
        self.exchanges = range_exchanges(self.dataset)

    def agreement(self) -> ProviderAgreement:
        return ProviderAgreement(
            self.dataset.chain_id,
            self.dataset.start_block,
            self.dataset.end_block,
            ("provider-a", "provider-b"),
            self.dataset.content_hash,
        )

    def test_quorum_evidence_is_required_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_store = EvidenceStore(Path(directory) / "evidence.db")
            chain_store = CanonicalChainStore(Path(directory) / "chain.db")
            verified = persist_and_verify_block_range(
                evidence_store,
                self.dataset,
                self.exchanges,
                agreement=self.agreement(),
                peer_captures=((replace(self.dataset, source="provider-b"), self.exchanges),),
                retrieved_at="2026-09-04T00:00:00+00:00",
            )
            result = apply_verified_block_range(
                chain_store,
                stream_id="pool-manager",
                verified=verified,
            )
            self.assertEqual(verified.trust, IngestionTrust.PROVIDER_QUORUM)
            self.assertEqual(len(verified.peer_evidence), 1)
            self.assertEqual(evidence_store.get(verified.peer_evidence[0]), self.dataset.payload)
            self.assertEqual(result.chain.canonical_tip, self.dataset.blocks[-1].ref)
            self.assertEqual(chain_store.logs("pool-manager", 4663), self.dataset.logs)
            evidence_store.close()
            chain_store.close()

    def test_provider_names_alone_cannot_grant_quorum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_store = EvidenceStore(Path(directory) / "evidence.db")
            with self.assertRaisesRegex(ValueError, "raw peer captures"):
                persist_and_verify_block_range(
                    evidence_store, self.dataset, self.exchanges, agreement=self.agreement()
                )
            evidence_store.close()

    def test_fails_closed_without_quorum_or_explicit_research_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_store = EvidenceStore(Path(directory) / "evidence.db")
            with self.assertRaisesRegex(ValueError, "requires provider agreement"):
                persist_and_verify_block_range(
                    evidence_store,
                    self.dataset,
                    self.exchanges,
                )
            verified = persist_and_verify_block_range(
                evidence_store,
                self.dataset,
                self.exchanges,
                research_single_provider=True,
                retrieved_at="2026-09-04T00:00:00+00:00",
            )
            self.assertEqual(verified.trust, IngestionTrust.SINGLE_PROVIDER_RESEARCH)
            evidence_store.close()

    def test_rejects_agreement_for_another_range(self) -> None:
        wrong = ProviderAgreement(
            4663,
            9,
            10,
            ("provider-a", "provider-b"),
            self.dataset.content_hash,
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence_store = EvidenceStore(Path(directory) / "evidence.db")
            with self.assertRaisesRegex(ValueError, "does not attest"):
                persist_and_verify_block_range(
                    evidence_store,
                    self.dataset,
                    self.exchanges,
                    agreement=wrong,
                )
            evidence_store.close()


if __name__ == "__main__":
    unittest.main()
