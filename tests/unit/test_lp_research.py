"""A managed research start joins acquisition, raw replay and diagnostic refusal."""

import json
from pathlib import Path

import pytest

from oasis_agent.models.common import canonical_json
from scripts import run_lp_research as research
from scripts import run_pool_state_observer as observer
from tests.unit import test_market_window as fixture
from tests.unit.test_pool_state_observer import clock  # noqa: F401 - shared RPC budget clock


class MarketPoster(fixture.StatePoster):
    def post(self, raw):
        request = json.loads(raw)
        if request["method"] == "eth_getLogs":
            return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": []})
        if request["method"] == "eth_getBlockByNumber" and request["params"][0] == "latest":
            request["params"][0] = hex(102)
            raw = canonical_json(request)
        return super().post(raw)


def setup():
    config = observer.Config(
        chain_id=fixture.CHAIN,
        pool_id=fixture.POOL_ID,
        state_view=fixture.VIEW,
        pool_manager=fixture.MANAGER,
        state_view_runtime_keccak=fixture.PIN,
        pool_manager_runtime_keccak=fixture.PIN,
        public_request_cap=27,
        paid_request_cap=25,
        public_interval_ms=1000,
        paid_interval_ms=3000,
        max_age_seconds=300,
        market=observer.MarketConfig(
            pool=fixture.POOL, blocks=3, horizon_seconds=1, max_lateness_seconds=1
        ),
    )
    posters = {
        source: MarketPoster(fixture.blocks(100, 3), (fixture.Q96,) * 3)
        for source in ("public", "paid")
    }
    return config, posters


@pytest.mark.usefixtures("clock")
def test_one_start_produces_replayed_reports_without_promoting_strategy(tmp_path):
    config, posters = setup()
    out = tmp_path / "run"
    assert research.run(config, posters, out, now=1102)
    status = json.loads((out / "status.json").read_bytes())
    assert status["stage"] == "completed"
    assert status["coverage_passed"] is False
    assert status["execution_eligible"] is False
    assert json.loads((out / "economics.json").read_bytes())["matured_count"] == 0
    assert (
        "development_coverage_below_21_days"
        in json.loads((out / "coverage.json").read_bytes())["reasons"]
    )
    with pytest.raises(FileExistsError):
        research.run(config, posters, out, now=1102)


@pytest.mark.usefixtures("clock")
def test_rpc_failure_retains_halt_without_zero_filled_economics(tmp_path):
    class Broken:
        def post(self, raw):
            raise ValueError("synthetic-sensitive-endpoint")

    config, posters = setup()
    posters["paid"] = Broken()
    out = tmp_path / "run"
    assert not research.run(config, posters, out, now=1102)
    assert json.loads((out / "status.json").read_bytes())["stage"] == "halted"
    assert not (out / "economics.json").exists()
    assert b"synthetic-sensitive-endpoint" not in (out / "status.json").read_bytes()
    assert (out / "capture/evidence.db").exists()


def test_example_is_a_bounded_market_config():
    config = observer.Config.from_json(
        Path("configs/research.base-eth-usdc.example.json").read_bytes()
    )
    assert config.market is not None
    assert config.chain_id == 8453
    assert config.public_request_cap == 20 and config.paid_request_cap == 18


@pytest.mark.usefixtures("clock")
def test_stop_retains_partial_capture_and_stopped_status(tmp_path):
    class StopRequested:
        def post(self, raw):
            raise KeyboardInterrupt

    config, posters = setup()
    posters["paid"] = StopRequested()
    out = tmp_path / "run"
    assert not research.run(config, posters, out, now=1102)
    assert json.loads((out / "status.json").read_bytes())["stage"] == "stopped"
    assert (out / "capture/evidence.db").exists()
    assert not (out / "economics.json").exists()
