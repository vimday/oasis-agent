"""Historical chunks resume from raw proofs and preserve one request budget."""

import json
import sqlite3
import sys
from dataclasses import replace
from itertools import count

import pytest

from oasis_agent.rpc.budget import BudgetedPoster
from oasis_agent.shadow.market_archive import ArchiveConfig
from scripts import capture_lp_market_archive as archive
from tests.unit import test_market_window as fixture
from tests.unit.test_lp_research import MarketPoster, setup


@pytest.mark.parametrize("transport", ["httpx", "curl"])
def test_cli_transport_selection_and_cleanup(tmp_path, archive_inputs, monkeypatch, transport):
    config, _, _ = archive_inputs
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    created = []

    class FakeHttp:
        def __init__(self, url):
            self.client = self
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    class FakeCurl:
        def __init__(self, url):
            created.append(self)

    def capture_stub(config, posters, output, **kwargs):
        expected = FakeCurl if transport == "curl" else FakeHttp
        assert all(type(p) is expected for p in posters.values())
        assert kwargs["max_chunks"] == 1
        assert len(set(kwargs["endpoint_hashes"].values())) == 2
        return {"stage": "partial"}

    monkeypatch.setattr(archive, "Http", FakeHttp)
    monkeypatch.setattr(archive, "CurlPoster", FakeCurl)
    monkeypatch.setattr(archive, "capture", capture_stub)
    monkeypatch.setenv("BASE_RPC_PRIMARY", "https://a.example")
    monkeypatch.setenv("BASE_RPC_SECONDARY", "https://b.example")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_lp_market_archive",
            "--config",
            str(config_path),
            "--output",
            str(tmp_path / "archive"),
            "--transport",
            transport,
        ],
    )
    archive.main()
    assert len(created) == 2
    if transport == "httpx":
        assert all(p.closed for p in created)


@pytest.fixture
def archive_inputs(monkeypatch):
    ticks = count(10000, 10000)
    monkeypatch.setattr(
        archive,
        "BudgetedPoster",
        lambda http, path, policies, source: BudgetedPoster(
            http, path, policies, source, clock=lambda: next(ticks)
        ),
    )
    observer, _ = setup()
    observer = observer.model_copy(
        update={"market": observer.market.model_copy(update={"blocks": 2})}
    )
    config = ArchiveConfig(
        observer=observer, start_block=100, end_block=103, max_requests_per_provider=36
    )
    posters = {
        source: MarketPoster(fixture.blocks(100, 4), (fixture.Q96,) * 4)
        for source in ("public", "paid")
    }
    hashes = {"public": "1" * 64, "paid": "2" * 64}
    return config, posters, hashes


def test_contiguous_resume_replays_and_does_not_refetch_completed_chunks(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    out = tmp_path / "archive"
    first = archive.capture(config, posters, out, endpoint_hashes=hashes)
    assert first["stage"] == "partial" and first["chunks_completed"] == 1
    assert first["request_counts"] == {"public": 18, "paid": 18}
    assert first["remaining_requests"] == {"public": 18, "paid": 18}
    assert first["minimum_requests_to_finish_per_provider"] == 18
    assert first["budget_can_finish_without_failures"] is True
    original = (out / "chunk-0000.json").read_bytes()
    final = archive.capture(config, posters, out, endpoint_hashes=hashes)
    assert final["stage"] == "completed" and final["chunks_completed"] == 2
    assert final["window_ready"] is True and final["coverage_passed"] is False
    assert final["remaining_requests"] == {"public": 0, "paid": 0}
    assert final["minimum_requests_to_finish_per_provider"] == 0
    window = json.loads((out / "market-window.json").read_bytes())
    assert len(window["prices"]) == 4 and len(window["input_references"]) == 8
    assert window["prices"][0]["block"]["number"] == 100
    assert window["prices"][-1]["block"]["number"] == 103
    assert (out / "chunk-0000.json").read_bytes() == original
    usage = (out / "budget.db").read_bytes()
    assert archive.capture(config, posters, out, endpoint_hashes=hashes)["stage"] == "completed"
    assert (out / "budget.db").read_bytes() == usage
    assert final["execution_eligible"] is False


def test_short_verified_archive_reports_maturity_requirement(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    market = config.observer.market.model_copy(
        update={"horizon_seconds": 300, "max_lateness_seconds": 60}
    )
    config = config.model_copy(
        update={"observer": config.observer.model_copy(update={"market": market})}
    )
    out = tmp_path / "archive"
    status = archive.capture(config, posters, out, endpoint_hashes=hashes, max_chunks=2)
    headers = fixture.blocks(100, 4)
    assert status["stage"] == "completed" and status["window_ready"] is False
    assert status["analysis_detail"] == {
        "code": "insufficient_maturity_tail",
        "observed_span_seconds": headers[-1].ref.timestamp - headers[0].ref.timestamp,
        "required_tail_seconds": 360,
    }
    assert not (out / "economics.json").exists()
    assert not status["execution_eligible"]


def test_other_analysis_errors_are_redacted_not_classified_as_short_history(
    tmp_path, archive_inputs, monkeypatch
):
    config, posters, hashes = archive_inputs

    def refuse(*args, **kwargs):
        raise ValueError("private endpoint details")

    monkeypatch.setattr(archive, "build_archive_window", refuse)
    status = archive.capture(
        config, posters, tmp_path / "archive", endpoint_hashes=hashes, max_chunks=2
    )
    assert status["window_ready"] is False
    assert status["analysis_detail"] == {"code": "analysis_or_output_failure"}
    assert "private" not in json.dumps(status)


def test_resume_refuses_changed_binding_missing_budget_and_raw_proof(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    out = tmp_path / "archive"
    archive.capture(config, posters, out, endpoint_hashes=hashes)
    with pytest.raises(ValueError, match="binding changed"):
        archive.capture(config, posters, out, endpoint_hashes=hashes | {"paid": "3" * 64})
    path = out / "chunk-0000.json"
    raw = json.loads(path.read_bytes())
    raw["prices"][0]["content_hash"] = "0" * 64
    path.write_text(json.dumps(raw))
    with pytest.raises((ValueError, KeyError)):
        archive.capture(config, posters, out, endpoint_hashes=hashes)
    (out / "budget.db").unlink()
    with pytest.raises(ValueError, match="lost evidence or budget"):
        archive.capture(config, posters, out, endpoint_hashes=hashes)


def test_deleted_acknowledged_tail_is_not_silently_refetched(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    out = tmp_path / "archive"
    archive.capture(config, posters, out, endpoint_hashes=hashes, max_chunks=2)
    usage = (out / "budget.db").read_bytes()
    (out / "chunk-0001.json").unlink()
    with pytest.raises(ValueError, match="checkpoint contradicts"):
        archive.capture(config, posters, out, endpoint_hashes=hashes)
    assert (out / "budget.db").read_bytes() == usage


def test_crash_after_chunk_write_before_status_can_resume(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    out = tmp_path / "archive"
    archive.capture(config, posters, out, endpoint_hashes=hashes)
    status_path = out / "status.json"
    status = json.loads(status_path.read_bytes())
    status["chunks_completed"] = 0
    status_path.write_text(json.dumps(status))
    result = archive.capture(config, posters, out, endpoint_hashes=hashes)
    assert result["stage"] == "completed" and result["chunks_completed"] == 2


def test_operator_stop_retains_partial_wire_and_resumes_original_budget(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    config = config.model_copy(update={"max_requests_per_provider": 54})
    out = tmp_path / "archive"

    class InterruptedPoster:
        calls = 0

        def post(self, payload):
            self.calls += 1
            if self.calls == 4:
                raise KeyboardInterrupt
            return posters["public"].post(payload)

    status = archive.capture(
        config, posters | {"public": InterruptedPoster()}, out, endpoint_hashes=hashes
    )
    assert status["stage"] == "stopped" and status["chunks_completed"] == 0
    assert not (out / "chunk-0000.json").exists()
    with sqlite3.connect(out / "evidence.db") as db:
        assert (
            db.execute("SELECT COUNT(*) FROM evidence WHERE kind='market_archive_wire'").fetchone()[
                0
            ]
            == 1
        )
    status = archive.capture(config, posters, out, endpoint_hashes=hashes, max_chunks=2)
    assert status["stage"] == "completed" and status["chunks_completed"] == 2
    with sqlite3.connect(out / "budget.db") as db:
        # Failed requests spend budget; recovery cannot roll it back.
        assert db.execute("SELECT COUNT(*) FROM rpc_usage").fetchone()[0] == 76


def test_budget_exhaustion_never_skips_or_resets_chunk(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    config = config.model_copy(update={"max_requests_per_provider": 18})
    out = tmp_path / "archive"
    first = archive.capture(config, posters, out, endpoint_hashes=hashes, max_chunks=2)
    assert first["stage"] == "halted" and first["reason"] == "insufficient_remaining_budget"
    assert first["budget_can_finish_without_failures"] is False
    assert first["chunks_completed"] == 1
    assert not (out / "chunk-0001.json").exists()
    second = archive.capture(config, posters, out, endpoint_hashes=hashes)
    assert second == first


def test_disagreement_keeps_partial_wire_without_admitting_a_chunk(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    posters["paid"] = MarketPoster(fixture.blocks(100, 4), (fixture.Q96 + 1,) * 4)
    out = tmp_path / "archive"
    status = archive.capture(config, posters, out, endpoint_hashes=hashes)
    assert status["stage"] == "halted" and status["chunks_completed"] == 0
    assert not (out / "chunk-0000.json").exists()
    assert (out / "evidence.db").exists()


def test_two_agreeing_chunks_on_disconnected_forks_are_not_joined(tmp_path, archive_inputs):
    config, posters, hashes = archive_inputs
    out = tmp_path / "archive"
    archive.capture(config, posters, out, endpoint_hashes=hashes)
    headers = list(fixture.blocks(100, 4))
    headers[2] = replace(headers[2], parent_hash="0x" + "ff" * 32)
    posters = {source: MarketPoster(tuple(headers), (fixture.Q96,) * 4) for source in posters}
    status = archive.capture(config, posters, out, endpoint_hashes=hashes)
    assert status["stage"] == "halted" and status["chunks_completed"] == 1
    assert not (out / "chunk-0001.json").exists()
    assert not (out / "market-window.json").exists()


def test_primary_horizon_can_mature_across_chunk_boundaries(tmp_path, archive_inputs):
    config, _, hashes = archive_inputs
    market = config.observer.market.model_copy(
        update={"horizon_seconds": 300, "max_lateness_seconds": 60}
    )
    config = config.model_copy(
        update={
            "observer": config.observer.model_copy(update={"market": market}),
            "end_block": 120,
            "max_requests_per_provider": 191,
        }
    )
    headers = tuple(
        replace(h, ref=replace(h.ref, timestamp=1100 + index * 30))
        for index, h in enumerate(fixture.blocks(100, 21))
    )
    log = fixture.swap_log(
        100,
        zero_for_one=True,
        amount0=-100,
        amount1=90,
        sqrt_price=fixture.Q96,
        liquidity=100,
        tick=0,
        fee=500,
        log_index=0,
    )
    log = replace(
        log, transaction_index=0, topics=(fixture.SWAP_TOPIC, fixture.POOL_ID, log.topics[2])
    )

    class WithSwap(MarketPoster):
        def post(self, raw):
            request = json.loads(raw)
            if request["method"] == "eth_getLogs":
                query = request["params"][0]
                result = []
                if int(query["fromBlock"], 16) <= 100 <= int(query["toBlock"], 16):
                    result = [
                        {
                            "address": log.address,
                            "topics": list(log.topics),
                            "data": log.data,
                            "blockNumber": hex(log.block_number),
                            "blockHash": log.block_hash,
                            "transactionHash": log.transaction_hash,
                            "transactionIndex": "0x0",
                            "logIndex": "0x0",
                            "removed": False,
                        }
                    ]
                return json.dumps(
                    {"jsonrpc": "2.0", "id": request["id"], "result": result}
                ).encode()
            return super().post(raw)

    posters = {source: WithSwap(headers, (fixture.Q96,) * 21) for source in ("public", "paid")}
    out = tmp_path / "archive"
    status = archive.capture(config, posters, out, endpoint_hashes=hashes, max_chunks=11)
    assert status["stage"] == "completed" and status["window_ready"]
    window = json.loads((out / "market-window.json").read_bytes())
    assert len(window["prices"]) == 21 and len(window["matured"]) == 1
    event = window["matured"][0]
    assert event["event_block"]["number"] == 100
    assert event["value"]["future_price_x192"]["block"]["number"] == 110
    coverage = json.loads((out / "coverage.json").read_bytes())
    assert "price_lookback_below_600s" not in coverage["reasons"]
    assert "fewer_than_20_price_observations" not in coverage["reasons"]
    assert not coverage["coverage_passed"] and not coverage["out_of_sample_accepted"]
