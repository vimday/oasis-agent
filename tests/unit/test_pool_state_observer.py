"""Bounded observer integration, with no live HTTP calls."""

import json
from itertools import count

import pytest

from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetedPoster, RpcBudget
from scripts import run_pool_state_observer as observer
from tests.unit import test_market_window as market_fixture

from .test_pool_state_capture import (
    CHAIN_ID,
    MANAGER_HASH,
    POOL_ID,
    POOL_MANAGER,
    STATE_HASH,
    STATE_VIEW,
    Poster,
)


def config():
    return observer.Config(
        chain_id=CHAIN_ID,
        pool_id=POOL_ID,
        state_view=STATE_VIEW,
        pool_manager=POOL_MANAGER,
        state_view_runtime_keccak=STATE_HASH,
        pool_manager_runtime_keccak=MANAGER_HASH,
        public_request_cap=18,
        paid_request_cap=7,
        public_interval_ms=1000,
        paid_interval_ms=3000,
        max_age_seconds=60,
    )


def test_impossible_pacing_refused_before_io(tmp_path):
    cfg = config().model_copy(update={"max_age_seconds": 20})
    # Seven reads per source require at least 6 * (1 + 3) seconds,
    # even excluding head discovery and network latency.
    with pytest.raises(ValueError, match="pacing cannot satisfy"):
        observer.sample(
            cfg,
            {"public": object(), "paid": object()},
            budget_path=tmp_path / "budget.db",
            output=tmp_path / "out",
        )
    assert not (tmp_path / "budget.db").exists()
    assert not (tmp_path / "out").exists()


@pytest.fixture
def clock(monkeypatch):
    ticks = count(10000, 10000)
    monkeypatch.setattr(
        observer,
        "BudgetedPoster",
        lambda http, path, policies, source: BudgetedPoster(
            http, path, policies, source, clock=lambda: next(ticks)
        ),
    )


def test_single_source_restart_spending_and_missing_economics(tmp_path, clock):
    budget = tmp_path / "budget.db"
    for index in (1, 2):
        report = observer.sample(
            config(),
            {"public": Poster()},
            budget_path=budget,
            output=tmp_path / str(index),
            now=1001,
        )
        assert report["status"] == "observed"
        assert report["trust"] == "single_source_research"
        assert report["sample_requests_spent"] == {"public": 9, "paid": 0}
        assert report["campaign_requests_spent"]["public"] == index * 9
        assert report["lp_action"] == "unavailable"
        assert "complete_cost_model" in report["missing_lp_inputs"]
        assert report["execution_eligible"] is False
    with pytest.raises(ValueError, match="remaining"):
        observer.sample(
            config(), {"public": Poster()}, budget_path=budget, output=tmp_path / "third", now=1001
        )
    with RpcBudget(budget, config().policies()) as ledger:
        assert ledger.counts() == {"public": 18, "paid": 0}


def test_dual_source_and_disagreement(tmp_path, clock):
    report = observer.sample(
        config(),
        {"public": Poster(), "paid": Poster()},
        budget_path=tmp_path / "a.db",
        output=tmp_path / "a",
        now=1001,
    )
    assert report["status"] == "observed"
    assert report["trust"] == "two_source_state"
    assert report["sample_requests_spent"] == {"public": 9, "paid": 7}
    result = observer.sample(
        config(),
        {"public": Poster(), "paid": Poster(sqrt_price_x96=2**97)},
        budget_path=tmp_path / "b.db",
        output=tmp_path / "b",
        now=1001,
    )
    assert result["status"] == "halt"
    assert len(result["wire_evidence"]) == 3
    assert "trust" not in result


def test_stale_head_stops_before_expensive_reads(tmp_path, clock):
    report = observer.sample(
        config(),
        {"public": Poster(), "paid": Poster()},
        budget_path=tmp_path / "quota.db",
        output=tmp_path / "stale",
        now=2000,
    )
    assert report["status"] == "halt"
    assert report["sample_requests_spent"] == {"public": 2, "paid": 0}


def test_env_loader_never_executes_shell_and_ignores_unused_paid_value(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        'ROBINHOOD_RPC_URL="https://public.invalid"\n'
        'ROBINHOOD_ALCHEMY_RPC_URL="broken paid configuration\n'
        "UNRELATED_SECRET=not-inspected\n"
    )
    assert observer.env_urls(path, secondary=False) == {"public": "https://public.invalid"}
    with pytest.raises(ValueError):
        observer.env_urls(path, secondary=True)
    path.write_text(
        "ROBINHOOD_RPC_URL=https://public.invalid\nROBINHOOD_RPC_URL=https://other.invalid\n"
    )
    with pytest.raises(ValueError):
        observer.env_urls(path, secondary=False)


def test_failed_transport_counts_and_report_redacts_provider_details(tmp_path, clock):
    class Failure:
        def post(self, payload):
            raise OSError("private-provider-detail")

    report = observer.sample(
        config(),
        {"public": Failure()},
        budget_path=tmp_path / "quota.db",
        output=tmp_path / "failed",
        now=1001,
    )
    assert report["status"] == "halt"
    assert report["sample_requests_spent"] == {"public": 1, "paid": 0}
    assert "private-provider-detail" not in json.dumps(report)


def test_acquisition_to_mature_market_window_is_budgeted(tmp_path, clock):

    class MarketPoster(market_fixture.StatePoster):
        def post(self, raw):
            request = json.loads(raw)
            if request["method"] == "eth_getLogs":
                return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": []})
            if request["method"] == "eth_getBlockByNumber" and request["params"][0] == "latest":
                request["params"][0] = hex(102)
                raw = canonical_json(request)
            return super().post(raw)

    cfg = observer.Config(
        chain_id=market_fixture.CHAIN,
        pool_id=market_fixture.POOL_ID,
        state_view=market_fixture.VIEW,
        pool_manager=market_fixture.MANAGER,
        state_view_runtime_keccak=market_fixture.PIN,
        pool_manager_runtime_keccak=market_fixture.PIN,
        public_request_cap=27,
        paid_request_cap=25,
        public_interval_ms=1000,
        paid_interval_ms=3000,
        max_age_seconds=300,
        market=observer.MarketConfig(
            pool=market_fixture.POOL, blocks=3, horizon_seconds=1, max_lateness_seconds=1
        ),
    )
    posters = {
        source: MarketPoster(
            market_fixture.blocks(100, 3),
            (market_fixture.Q96, market_fixture.Q96, market_fixture.Q96),
        )
        for source in ("public", "paid")
    }
    report = observer.sample(
        cfg, posters, budget_path=tmp_path / "q.db", output=tmp_path / "run", now=1102
    )
    assert report["status"] == "observed"
    assert report["sample_requests_spent"] == {"public": 27, "paid": 25}
    assert report["market_window"]["markouts_complete"] is True
    assert "matured_swap_markouts" not in report["missing_lp_inputs"]
    assert report["execution_eligible"] is False
    with pytest.raises(ValueError, match="remaining"):
        observer.sample(
            cfg, posters, budget_path=tmp_path / "q.db", output=tmp_path / "next", now=1102
        )


def test_explicit_base_rpc_names_do_not_require_robinhood_credentials(tmp_path):
    path = tmp_path / "public.env"
    path.write_text("BASE_RPC_A=https://a.invalid\nBASE_RPC_B=https://b.invalid\n")
    assert observer.env_urls(
        path, secondary=True, primary_env="BASE_RPC_A", secondary_env="BASE_RPC_B"
    ) == {"public": "https://a.invalid", "paid": "https://b.invalid"}
    for name in ("PRIVATE_KEY", "BASE_RPC_KEY", "bad-name"):
        with pytest.raises(ValueError, match="environment names"):
            observer.env_urls(path, secondary=False, primary_env=name)
    with pytest.raises(ValueError, match="environment names"):
        observer.env_urls(
            path, secondary=True, primary_env="BASE_RPC_A", secondary_env="BASE_RPC_A"
        )
