"""Synthetic crash -> restart -> receipt recovery, with no real network or wallet.

The child actually exits without cleanup after a fixture submission, leaving the
production journal's durable reservation. Fixture signatures are not live permits.
"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_broadcast import (
    LpBroadcaster,
    expectation_from_sidecar,
    raw_bytes_from_decision,
)
from oasis_agent.execution.lp_receipts import gas_declarations, observe_receipt
from oasis_agent.farming.transaction_costs import replay_transaction_cost
from tests.unit.test_account_risk_reader import InventoryFixture, manifest
from tests.unit.test_lp_broadcast import FakeRpc, policy, signed_decision, signed_request
from tests.unit.test_lp_receipts import (
    OTHER_HASH,
    PRIMARY,
    SECONDARY,
    ReceiptFixture,
    entry,
    header,
    sessions,
)

CHILD = """
import json, os, sys
from pathlib import Path
from eth_hash.auto import keccak
from oasis_agent.execution.lp_broadcast import (
    LpBroadcaster, expectation_from_sidecar, raw_bytes_from_decision,
)
from tests.unit.test_lp_broadcast import policy, signed_decision, signed_request

class CrashAfterFixtureSubmission:
    def chain_id(self):
        return 4663
    def send_raw_transaction(self, raw_hex):
        # This method has no transport or socket; it only records a synthetic hash.
        Path(sys.argv[2]).write_text(json.dumps({
            'fixture_submission_count': 1,
            'transaction_hash': '0x' + keccak(bytes.fromhex(raw_hex[2:])).hex(),
            'synthetic_fixture': True,
        }))
        os._exit(73)

request = signed_request()
decision = signed_decision(request)
broadcaster = LpBroadcaster(policy(), Path(sys.argv[1]), rpc=CrashAfterFixtureSubmission())
broadcaster.broadcast(raw_bytes_from_decision(decision),
                      expectation=expectation_from_sidecar(request, decision))
raise AssertionError('fixture child must die before acknowledgement')
"""


@pytest.mark.parametrize("receipt_status", [0, 1], ids=["reverted", "successful"])
def test_actual_process_death_blocks_resend_until_and_after_receipt_recovery(
    tmp_path: Path, receipt_status: int
) -> None:
    root = Path(__file__).resolve().parents[2]
    journal_path = tmp_path / "broadcast.db"
    marker = tmp_path / "fixture-submission.json"
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(journal_path), str(marker)],
        cwd=root,
        env={"PATH": os.defpath, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 73, child.stderr.decode()
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    raw = raw_bytes_from_decision(decision)
    assert json.loads(marker.read_bytes()) == {
        "fixture_submission_count": 1,
        "transaction_hash": expectation.transaction_hash,
        "synthetic_fixture": True,
    }
    retry_rpc = FakeRpc(result=expectation.transaction_hash)
    refused_receipts = []
    with LpBroadcaster(policy(), journal_path, rpc=retry_rpc) as restarted:
        assert restarted.journal.open_transactions() == (expectation.transaction_hash,)
        assert restarted.broadcast(raw, expectation=expectation).outcome == "denied"
        assert retry_rpc.sent == []
        with closing(EvidenceStore(tmp_path / "receipts.db")) as store:
            for failure in ("missing", "disagreement", "reorg"):
                primary = ReceiptFixture(
                    transaction_hash=expectation.transaction_hash, status=receipt_status
                )
                secondary = ReceiptFixture(
                    transaction_hash=expectation.transaction_hash, status=receipt_status
                )
                if failure == "missing":
                    primary.has_receipt = False
                elif failure == "disagreement":
                    secondary.gas_used += 1
                else:
                    secondary.header_hash = OTHER_HASH
                with pytest.raises(ValueError):
                    observe_receipt(
                        store,
                        entry(),
                        transaction_hash=expectation.transaction_hash,
                        sessions=sessions({PRIMARY: primary, SECONDARY: secondary}),
                    )
                assert restarted.journal.open_transactions() == (expectation.transaction_hash,)
                assert restarted.broadcast(raw, expectation=expectation).outcome == "denied"
                assert retry_rpc.sent == []
                with closing(sqlite3.connect(tmp_path / "receipts.db")) as db:
                    assert db.execute("SELECT count(*) FROM evidence").fetchone() == (0,)
                refused_receipts.append(failure)
            observation = observe_receipt(
                store,
                entry(),
                transaction_hash=expectation.transaction_hash,
                sessions=sessions(
                    {
                        source: ReceiptFixture(
                            transaction_hash=expectation.transaction_hash, status=receipt_status
                        )
                        for source in (PRIMARY, SECONDARY)
                    }
                ),
            )
            assert observation.status == receipt_status
            assert observation.total_native == 200  # Reverts spend gas too.
            for ref in observation.evidence:
                cost = replay_transaction_cost(
                    store, ref, header=header(), transaction_hash=expectation.transaction_hash
                ).reconstruct()
                assert (cost.status, cost.total_native) == (receipt_status, 200)
            declared_manifest = manifest(InventoryFixture())
            declarations = gas_declarations(declared_manifest, (observation,))
            assert declarations == {entry().key: {"gas_units": 200, "non_trading_units": 0}}
            with pytest.raises(ValueError, match="one observation per transaction"):
                gas_declarations(declared_manifest, (observation, observation))
            restarted.journal.resolve(
                observation.transaction_hash,
                receipt_status=observation.status,
                block_number=observation.block_number,
            )
            assert restarted.journal.open_transactions() == ()
    # The second process restart revalidates the hash-linked resolution. A known
    # revert is terminal for these exact bytes and is never resent as a retry.
    with LpBroadcaster(policy(), journal_path, rpc=retry_rpc) as reopened:
        assert reopened.journal.open_transactions() == ()
        assert reopened.broadcast(raw, expectation=expectation).outcome == "denied"
        assert (
            reopened.journal.request_hash(expectation.transaction_hash) == expectation.request_hash
        )
        assert retry_rpc.sent == []
    assert raw not in journal_path.read_bytes()
    assert raw.hex().encode() not in journal_path.read_bytes()
    (tmp_path / "case-summary.json").write_text(
        json.dumps(
            {
                "synthetic_fixture": True,
                "external_network_calls": 0,
                "child_exit_code": child.returncode,
                "fixture_submission_count": 1,
                "resubmissions": len(retry_rpc.sent),
                "receipt_status": receipt_status,
                "refused_receipt_cases": refused_receipts,
                "two_provider_gas_units": observation.total_native,
                "gas_declarations": declarations,
                "execution_eligible": False,
            },
            sort_keys=True,
        )
    )
