import json
from dataclasses import replace

import pytest

from oasis_agent.farming.batch_settlement import reconcile_batch_settlement
from tests.unit.test_position_settlement import mutate
from tests.unit.test_transaction_payments import fixture


def test_batch_core_cash_conserves_each_token():
    report = json.loads(reconcile_batch_settlement(fixture()))
    assert len(report["positions"]) == 1
    assert all(v == 0 for v in report["pool_manager_residual_by_token"].values())
    assert report["individual_owner_cash_allocation"] == "unresolved"


def test_core_claim_mismatch_cannot_pass_cash_reconciliation():
    capture = fixture()

    def change(r):
        frame = r["result"]["calls"][0]
        frame["output"] = (
            "0x" + b"".join(x.to_bytes(16, "big", signed=True) for x in (-99, -200, 0, 0)).hex()
        )

    core = mutate(capture.core, 3, change)
    with pytest.raises(ValueError, match="residual"):
        reconcile_batch_settlement(replace(capture, core=core))
