"""Holder concentration diagnostics never authorize capital or trust imported labels."""

import json
import shutil
import subprocess
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from oasis_agent.launch.holders import assess_holders
from oasis_agent.launch.models import HolderObservation, HolderTarget, parse_target
from oasis_agent.server.app import create_app
from oasis_agent.server.config import load_server_config
from oasis_agent.shadow.sniper_runner import SniperJournal, evaluate, read_records
from scripts.check_dashboard import dashboard_source

from .test_launch import cycle, target


def policy():
    return HolderTarget.model_validate(
        target().model_dump()
        | {
            "schema": "oasis.launch-target.v3",
            "holder_limits": {
                "min_holders": 2,
                "max_single_bps": 6000,
                "max_top10_bps": 10000,
                "max_hhi_bps": 5200,
            },
        }
    )


def sample(source="provider_a", amounts=(60, 40)):
    observation = cycle().observations[0]
    return HolderObservation.model_validate(
        {
            **{
                key: observation.model_dump()[key]
                for key in (
                    "snapshot",
                    "retrieved_at",
                    "target_ca",
                    "runtime_hash",
                    "token_decimals",
                )
            },
            "source": source,
            "raw_evidence_digest": ("3" if source == "provider_a" else "4") * 64,
            "total_supply_base_units": 100,
            "complete": True,
            "balances": tuple(
                {"address": "0x" + f"{index + 1:040x}", "balance_base_units": amount}
                for index, amount in enumerate(amounts)
            ),
        }
    )


def with_holders(left=None, right=None):
    return cycle().model_copy(
        update={
            "holder_observations": (
                sample() if left is None else left,
                sample("provider_b") if right is None else right,
            )
        }
    )


def test_exact_threshold_and_unverified_proof_remain_closed():
    result = assess_holders(with_holders(), policy())
    assert result["metrics"] == {
        "holder_count": 2,
        "single_bps": 6000,
        "top10_bps": 10000,
        "hhi_bps": 5200,
    }
    assert result["reasons"] == ["holder_raw_proof_unverified", "holder_common_control_unresolved"]
    assert evaluate(with_holders(), policy())["execution_eligible"] is False


def test_order_independence_and_scale_invariance():
    baseline = assess_holders(with_holders(), policy())["metrics"]
    for scale in (1, 10**6, 10**18):
        pair = []
        for source in ("provider_a", "provider_b"):
            value = sample(source).model_dump()
            value["total_supply_base_units"] *= scale
            for item in value["balances"]:
                item["balance_base_units"] *= scale
            if source == "provider_b":
                value["balances"] = tuple(reversed(value["balances"]))
            pair.append(HolderObservation.model_validate(value))
        assert assess_holders(with_holders(*pair), policy())["metrics"] == baseline


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("complete", False, "holder_coverage_incomplete"),
        ("total_supply_base_units", 101, "holder_supply_unreconciled"),
        ("runtime_hash", "0x" + "9" * 64, "holder_identity_mismatch"),
        ("token_decimals", 6, "holder_identity_mismatch"),
        ("retrieved_at", 1003, "holder_time_incoherent"),
    ],
)
def test_invalid_paired_data_stands_down(field, value, reason):
    result = assess_holders(
        with_holders(
            sample().model_copy(update={field: value}),
            sample("provider_b").model_copy(update={field: value}),
        ),
        policy(),
    )
    assert reason in result["reasons"]
    assert result["stand_down"] is True
    assert result["metrics"] is None


def test_missing_duplicate_provider_disagreement_and_cross_chain():
    assert "holder_two_sources_required" in assess_holders(cycle(), policy())["reasons"]
    assert assess_holders(with_holders(sample(), sample()), policy())["stand_down"]
    assert assess_holders(with_holders(right=sample("provider_b", (59, 41))), policy())[
        "stand_down"
    ]
    bad = sample().model_dump()
    bad["snapshot"]["chain_id"] = 4663
    result = assess_holders(with_holders(left=HolderObservation.model_validate(bad)), policy())
    assert "holder_snapshot_mismatch" in result["reasons"]


def test_concentration_breach_and_rounding_are_conservative():
    pair = with_holders(sample(amounts=(61, 39)), sample("provider_b", (61, 39)))
    result = assess_holders(pair, policy())
    assert "holder_single_limit_exceeded" in result["reasons"]
    assert "holder_hhi_limit_exceeded" in result["reasons"]
    assert result["metrics"]["hhi_bps"] == 5242
    for total in range(2, 25):
        data = sample().model_dump()
        data.update(
            total_supply_base_units=total,
            balances=(
                {"address": "0x" + f"{i + 1:040x}", "balance_base_units": 1} for i in range(total)
            ),
        )
        data["balances"] = tuple(data["balances"])
        a = HolderObservation.model_validate(data)
        result = assess_holders(
            with_holders(a, a.model_copy(update={"source": "provider_b"})), policy()
        )
        assert result["metrics"]["hhi_bps"] == (10000 + total - 1) // total


@pytest.mark.parametrize("bad", [True, 1.5, -1, 0, 2**256])
def test_invalid_balance_units_rejected(bad):
    data = sample().model_dump()
    data["balances"][0]["balance_base_units"] = bad
    with pytest.raises(ValueError):
        HolderObservation.model_validate(data)


def test_duplicate_address_and_supply_overflow_rejected():
    data = sample().model_dump()
    data["balances"] = (data["balances"][0],) * 2
    with pytest.raises(ValueError):
        HolderObservation.model_validate(data)
    data = sample().model_dump() | {"total_supply_base_units": 99}
    with pytest.raises(ValueError):
        HolderObservation.model_validate(data)


def test_legacy_input_shape_and_v3_frozen_limits():
    assert "holder_observations" not in json.loads(cycle().model_dump_json())
    assert "holders" not in evaluate(cycle(), target())
    assert parse_target(policy().model_dump_json()) == policy()
    data = policy().model_dump(mode="json")
    del data["holder_limits"]
    with pytest.raises(ValueError):
        parse_target(json.dumps(data))


def test_journal_restart_and_tampering(tmp_path):
    path = tmp_path / "holders.db"
    journal = SniperJournal(path, policy())
    record = journal.append(with_holders())
    assert "HOLDERS" in record["gates"]
    journal.close()
    journal = SniperJournal(path, policy())
    try:
        assert journal.append(with_holders()) == record
        assert len(read_records(journal.db)) == 1
        journal.db.execute("UPDATE launch_decisions SET payload=?", (b"{}",))
        with pytest.raises(ValueError):
            read_records(journal.db)
    finally:
        journal.close()


def test_v3_holder_results_reach_existing_api(tmp_path):
    path = tmp_path / "launch.db"
    journal = SniperJournal(path, policy())
    expected = journal.append(with_holders())
    journal.close()
    config = load_server_config(None, environ={})
    config = replace(
        config,
        ops_token_required=False,
        databases=replace(config.databases, launch=path, ops=tmp_path / "ops.db"),
    )
    with TestClient(create_app(config, environ={})) as client:
        response = client.get("/api/v1/launch/dashboard")
        assert response.status_code == 200
        sections = response.json()["sections"]
        assert sections["decisions"]["items"] == [expected]
        candidate = sections["candidates"]["items"][0]
        assert candidate["holders"] == expected["holders"]
        assert candidate["gates"]["HOLDERS"]["passed"] is False
        assert candidate["gates"]["HOLDERS"]["reasons"] == sorted(expected["holders"]["reasons"])
        assert sections["targets"]["items"][0]["schema"] == "oasis.launch-target.v3"
        assert sections["decisions"]["execution_eligible"] is False


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"min_holders": 3}, "holder_count_below_minimum"),
        ({"max_top10_bps": 9999}, "holder_top10_limit_exceeded"),
        ({"max_single_bps": 5999}, "holder_single_limit_exceeded"),
        ({"max_hhi_bps": 5199}, "holder_hhi_limit_exceeded"),
    ],
)
def test_each_frozen_limit_is_enforced(updates, reason):
    data = policy().model_dump()
    data["holder_limits"].update(updates)
    assert reason in assess_holders(with_holders(), HolderTarget.model_validate(data))["reasons"]


@pytest.mark.parametrize(
    "updates",
    [
        {"evaluated_at": 1061},
        {"current_block": 106},
        {"current_block": 99},
    ],
)
def test_stale_and_future_holders_do_not_yield_metrics(updates):
    result = assess_holders(with_holders().model_copy(update=updates), policy())
    assert result["stand_down"] is True
    assert result["metrics"] is None
    assert "holder_stale_or_future_snapshot" in result["reasons"]


def test_empty_balances_cannot_pass_by_completeness_assertion():
    pair = with_holders(
        sample().model_copy(update={"balances": ()}),
        sample("provider_b").model_copy(update={"balances": ()}),
    )
    assert assess_holders(pair, policy())["metrics"] is None


def test_frontend_holder_missing_and_unverified_labels():
    node = shutil.which("node")
    assert node is not None
    source = dashboard_source()
    functions = source[source.index("function launchReason(") : source.index("let launchLoading")]
    program = (
        functions
        + r"""
const assert = require('node:assert/strict');
assert.match(launchHolders(null), /未验证/);
assert.match(launchHolders({metrics: {}}), /无完整/);
assert.match(launchHolders({metrics: {holder_count: 2, single_bps: 6000,
  top10_bps: 10000, hhi_bps: 5200}}), /导入数据.*未验证/);
assert.match(launchReason('holder_supply_unreconciled'), /未核平/);
assert.match(launchReason('holder_provider_disagreement'), /不一致/);
"""
    )
    subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
