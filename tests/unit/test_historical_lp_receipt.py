import json
import sqlite3

import pytest

from oasis_agent.errors import RpcError
from scripts.capture_historical_lp_receipt import Request, capture
from tests.unit.test_lp_receipts import PAYER, TRANSACTION, ReceiptFixture


def request(chain=8453, cap=32):
    return Request(
        **{
            "schema": "oasis.historical-receipt-input.v1",
            "chain_id": chain,
            "transaction_hash": TRANSACTION,
            "payer": PAYER,
            "sources": [
                {"alias": "a", "env": "PRIMARY_RPC"},
                {"alias": "b", "env": "SECONDARY_RPC"},
            ],
            "max_requests_per_provider": cap,
            "min_interval_ms": 0,
        }
    )


@pytest.mark.parametrize("chain", [1, 8453, 42161])
@pytest.mark.parametrize("status", [0, 1])
def test_historical_cost_needs_no_broadcast_and_resume_replays_without_io(tmp_path, chain, status):
    posters = {a: ReceiptFixture(chain_id=chain, status=status) for a in ("a", "b")}
    r = request(chain)
    out = tmp_path / "campaign"
    result = capture(r, posters, out, {"a": "1", "b": "2"})
    assert result["cost"]["status"] == status
    assert result["cost"]["total_native"] == (218 if chain == 8453 else 200)
    assert result["position_allocation"] == result["valuation"] == "unresolved"
    assert result["execution_eligible"] is False
    calls = [len(p.methods) for p in posters.values()]
    assert calls == ([8, 6] if chain == 8453 else [6, 4])
    assert capture(r, posters, out, {"a": "1", "b": "2"}) == result
    assert calls == [len(p.methods) for p in posters.values()]
    result["cost"]["total_native"] = 0
    (out / "report.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="differs from its proofs"):
        capture(r, posters, out, {"a": "1", "b": "2"})
    assert calls == [len(p.methods) for p in posters.values()]


@pytest.mark.parametrize("failure", ["payer", "cost", "missing", "hash"])
def test_conflict_refuses_cost_but_retains_raw_failure_samples(tmp_path, failure):
    posters = {a: ReceiptFixture(chain_id=8453) for a in ("a", "b")}
    if failure == "payer":
        posters["b"].payer = "0x" + "33" * 20
    elif failure == "cost":
        posters["b"].l1_fee += 1
    elif failure == "missing":
        posters["b"].has_receipt = False
    else:
        posters["b"].header_hash = "0x" + "ee" * 32
    out = tmp_path / "campaign"
    with pytest.raises(ValueError):
        capture(request(), posters, out, {"a": "1", "b": "2"})
    assert not (out / "report.json").exists()
    state = json.loads((out / "status.json").read_bytes())
    assert state["stage"] == "halted"
    assert state["request_counts"]["a"] == 8
    with sqlite3.connect(out / "evidence.db") as db:
        assert (
            db.execute("select count(*) from evidence where kind='transaction_cost'").fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "select count(*) from evidence where kind='historical_receipt_wire'"
            ).fetchone()[0]
            == 3
        )


def test_failed_request_charged_and_budget_cannot_be_reset_or_exceeded(tmp_path):
    class FailedPoster:
        def post(self, payload):
            raise OSError("credential-do-not-copy")

    out = tmp_path / "campaign"
    posters = {"a": FailedPoster(), "b": ReceiptFixture(chain_id=8453)}
    with pytest.raises(RpcError):
        capture(request(cap=8), posters, out, {"a": "1", "b": "2"})
    state = json.loads((out / "status.json").read_bytes())
    assert state["request_counts"] == {"a": 1, "b": 0}
    assert "credential" not in (out / "status.json").read_text()
    with pytest.raises(ValueError, match="insufficient remaining budget"):
        capture(request(cap=8), posters, out, {"a": "1", "b": "2"})
    assert json.loads((out / "status.json").read_bytes())["request_counts"] == {"a": 1, "b": 0}
    with pytest.raises(ValueError, match="binding changed"):
        capture(request(cap=32), posters, out, {"a": "1", "b": "2"})
    (out / "budget.db").unlink()
    with pytest.raises(ValueError, match="lost budget or evidence"):
        capture(request(cap=8), posters, out, {"a": "1", "b": "2"})
