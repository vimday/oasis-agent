"""Complete only an unspent paid leg using existing independent raw evidence."""

import json
import sys
from dataclasses import replace

import pytest

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.farming.transaction_costs import transaction_cost_head, transaction_cost_tail
from oasis_agent.live.provider_acceptance import extra_requests
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts import complete_retained_trace_quorum as cli
from tests.unit.test_live_provider_acceptance import Fixture
from tests.unit.test_lp_receipts import PRIMARY, sessions


def retain(path, value):
    value["evidence_hash"] = sha256_hex(canonical_json(value))
    path.write_text(json.dumps(value))
    return path


def inputs(tmp_path):
    f = Fixture()

    def wire(requests):
        rpc, record, _ = sessions({PRIMARY: replace(f, methods=[])})(PRIMARY)
        for method, params in requests:
            rpc.request(method, params)
        return {
            "request": wire_envelope(record.exchanges, response=False),
            "response": wire_envelope(record.exchanges, response=True),
        }

    body = wire(
        transaction_cost_head(f.block, f.transaction_hash)
        + transaction_cost_tail(f.block, gas=f.gas_used)
        + [extra_requests(f.block, f.transaction_hash)[0]]
    )
    peer = wire(
        [
            ("eth_chainId", []),
            ("eth_getTransactionReceipt", [f.transaction_hash]),
            ("eth_getBlockByNumber", [hex(f.block.number), False]),
            extra_requests(f.block, f.transaction_hash)[1],
        ]
    )
    b = retain(tmp_path / "body.json", {"captures": [{"source": "drpc", **body}]})
    p = retain(
        tmp_path / "peer.json",
        {
            "source": "quicknode-demo",
            "endpoint": "https://docs-demo.base-mainnet.quiknode.pro/",
            **peer,
        },
    )
    return f, p, b


def test_complete_only_paid_leg_then_refuse_another_output(tmp_path, monkeypatch):
    f, p, b = inputs(tmp_path)
    monkeypatch.setattr(
        cli, "POLICIES", (BudgetPolicy("drpc", 12, 0), BudgetPolicy("alchemy", 12, 0))
    )
    path = tmp_path / "budget.db"
    with RpcBudget(path, cli.POLICIES) as budget:
        budget.reserve("drpc", now_ms=1)
    monkeypatch.setattr(cli, "_UrlLibPoster", lambda *_: f)
    url = "https://base-mainnet.g.alchemy.com/v2/fixture"
    result = cli.complete(tmp_path / "out", p, b, url, budget_path=path)
    assert result["passed"] and result["result"]["trace_frames"] == 2
    assert result["request_counts"] == {"drpc": 1, "alchemy": 4}
    assert len(f.methods) == 4 and not result["execution_eligible"]
    with pytest.raises(ValueError, match="already attempted"):
        cli.complete(tmp_path / "out2", p, b, url, budget_path=path)
    assert len(f.methods) == 4 and not (tmp_path / "out2").exists()

    # Replay must work with both network and credential access forbidden.
    def forbidden(*args, **kwargs):
        raise AssertionError("offline replay attempted I/O")

    monkeypatch.setattr(cli, "_UrlLibPoster", forbidden)
    monkeypatch.setattr(cli, "_environment", forbidden)
    monkeypatch.setattr(cli, "RpcBudget", forbidden)
    assert cli.replay_completion(tmp_path / "out", p, b) == result
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay", "--peer", str(p), "--body", str(b), "--out", str(tmp_path / "out"), "--replay"],
    )
    cli.main()
    result["result"]["trace_frames"] = 999
    result.pop("evidence_hash")
    retain(tmp_path / "out/report.json", result)
    with pytest.raises(ValueError, match="trace/receipt mismatch"):
        cli.replay_completion(tmp_path / "out", p, b)


def test_competing_completion_refused_before_first_charge(tmp_path, monkeypatch):
    f, p, b = inputs(tmp_path)
    monkeypatch.setattr(
        cli, "POLICIES", (BudgetPolicy("drpc", 12, 0), BudgetPolicy("alchemy", 12, 0))
    )
    path = tmp_path / "budget.db"
    with RpcBudget(path, cli.POLICIES):
        pass
    url = "https://base-mainnet.g.alchemy.com/v2/fixture"

    def competing(*args):
        with RpcBudget(path, cli.POLICIES) as ledger:
            assert ledger.counts()["alchemy"] == 0
        with pytest.raises(ValueError, match="already claimed"):
            cli.complete(tmp_path / "racer", p, b, url, budget_path=path)
        return f

    monkeypatch.setattr(cli, "_UrlLibPoster", competing)
    assert cli.complete(tmp_path / "out", p, b, url, budget_path=path)["passed"]
    assert len(f.methods) == 4 and not (tmp_path / "racer").exists()


def test_no_new_budget_or_forged_report_can_trigger_paid_call(tmp_path, monkeypatch):
    _, p, b = inputs(tmp_path)

    def forbidden(*args):
        raise AssertionError("no paid call")

    monkeypatch.setattr(cli, "_UrlLibPoster", forbidden)
    with pytest.raises(ValueError, match="existing acceptance budget"):
        cli.complete(
            tmp_path / "out",
            p,
            b,
            "https://base-mainnet.g.alchemy.com/v2/fixture",
            budget_path=tmp_path / "absent.db",
        )
    assert not (tmp_path / "absent.db").exists()
    value = json.loads(p.read_text())
    value["source"] = "alchemy"
    p.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest"):
        cli.retained_inputs(p, b)


@pytest.mark.parametrize("fault", ["single", "input", "value", "block", "revert"])
def test_paid_disagreement_stays_refused_without_retry(tmp_path, monkeypatch, fault):
    _, p, b = inputs(tmp_path)
    f = Fixture(fault=fault)
    monkeypatch.setattr(
        cli, "POLICIES", (BudgetPolicy("drpc", 12, 0), BudgetPolicy("alchemy", 12, 0))
    )
    path = tmp_path / "budget.db"
    with RpcBudget(path, cli.POLICIES):
        pass
    monkeypatch.setattr(cli, "_UrlLibPoster", lambda *_: f)
    # A block fault affects transaction lookup only, so mutate the receipt explicitly.
    if fault == "block":
        f.block_hash = "0x" + "ee" * 32
    r = cli.complete(
        tmp_path / "out", p, b, "https://base-mainnet.g.alchemy.com/v2/fixture", budget_path=path
    )
    assert not r["passed"] and r["request_counts"]["alchemy"] == 4
    assert len(f.methods) == 4
