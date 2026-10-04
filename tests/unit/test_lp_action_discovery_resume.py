"""Resume under the original ledger; neither failures nor restarts reset usage."""

import json
from itertools import count
from subprocess import CompletedProcess

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts import capture_lp_market_archive as archive_cli
from scripts import resume_lp_action_discovery as cli
from tests.unit import test_market_archive
from tests.unit.test_segmented_logs import Poster


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    config, posters, hashes = test_market_archive.archive_inputs.__wrapped__(monkeypatch)
    archive = tmp_path / "archive"
    archive_cli.capture(config, posters, archive, endpoint_hashes=hashes, max_chunks=1000)
    checkpoint = tmp_path / "complete-checkpoint-sha256.json"
    checkpoint.write_bytes(
        canonical_json(
            {
                str(p): sha256_hex(p.read_bytes())
                for p in (
                    archive / "manifest.json",
                    archive / "evidence.db",
                    *sorted(archive.glob("chunk-*.json")),
                )
            }
        )
    )
    out = tmp_path / "diagnostic"
    out.mkdir()
    manifest = {
        "schema": "oasis.lp-action-discovery-campaign.v1",
        "archive_manifest_sha256": sha256_hex((archive / "manifest.json").read_bytes()),
        "archive_complete_checkpoint_sha256": sha256_hex(checkpoint.read_bytes()),
        "endpoint_hashes": hashes,
        "chain_id": config.observer.chain_id,
        "start_block": config.start_block,
        "end_block": config.end_block,
        "address": config.observer.pool_manager,
        "topics": [
            "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec",
            config.observer.pool_id,
        ],
        "max_requests_per_provider": 16,
        "min_interval_ms": 3000,
    }
    (out / "manifest.json").write_bytes(canonical_json(manifest))
    EvidenceStore(out / "evidence.db").close()
    policies = tuple(BudgetPolicy(a, 16, 3000) for a in hashes)
    with RpcBudget(out / "budget.db", policies) as budget:
        for a in hashes:
            for n in range(6):
                assert budget.reserve(a, now_ms=(n + 1) * 10000) == 0
    ticks = count(100000, 10000)
    monkeypatch.setattr(
        cli,
        "BudgetedPoster",
        lambda http, path, ps, s: BudgetedPoster(
            http,
            path,
            ps,
            s,
            clock=lambda: next(ticks),
        ),
    )
    headers = posters["public"].headers

    class EmptyPoster(Poster):
        def __init__(self):
            super().__init__(headers)
            self.fail = False

        def post(self, raw):
            r = json.loads(raw)
            if r["method"] == "eth_getLogs":
                self.calls.append((r["method"], r["params"]))
                response = {"jsonrpc": "2.0", "id": r["id"]}
                if self.fail:
                    response["error"] = {"code": -32600, "message": "fixture refusal"}
                else:
                    response["result"] = []
                return canonical_json(response)
            return super().post(raw)

    yield out, archive, {s: EmptyPoster() for s in hashes}, hashes, policies


def test_fully_replayed_resume_is_zero_rpc_and_budget_unchanged(campaign):
    out, archive, posters, hashes, policies = campaign
    report = cli.resume(out, archive, posters, hashes)
    assert report["log_range_complete"] and not report["cash_window_complete"]
    assert report["event_count"] == 0 and not report["execution_eligible"]
    with RpcBudget(out / "budget.db", policies) as budget:
        assert budget.counts() == {"public": 10, "paid": 10}
    before = (out / "budget.db").read_bytes()
    calls = {s: len(p.calls) for s, p in posters.items()}
    assert cli.resume(out, archive, posters, hashes) == report
    assert (out / "budget.db").read_bytes() == before
    assert calls == {s: len(p.calls) for s, p in posters.items()}


def test_partial_provider_failure_keeps_proof_and_spent_budget(campaign):
    out, archive, posters, hashes, policies = campaign
    posters["paid"].fail = True
    with pytest.raises(RpcError):
        cli.resume(out, archive, posters, hashes)
    assert (out / "segmented-public.json").exists()
    assert not (out / "segmented-report.json").exists()
    with RpcBudget(out / "budget.db", policies) as budget:
        assert budget.counts() == {"public": 10, "paid": 9}
    assert json.loads((out / "segmented-status.json").read_bytes())["stage"] == "partial_or_refused"
    calls = len(posters["public"].calls)
    posters["paid"].fail = False
    cli.resume(out, archive, posters, hashes)
    assert len(posters["public"].calls) == calls
    with RpcBudget(out / "budget.db", policies) as budget:
        assert budget.counts() == {"public": 10, "paid": 13}


def test_one_provider_complete_is_partial_and_never_quorum_acceptance(campaign):
    out, archive, posters, hashes, policies = campaign
    report = cli.resume(out, archive, posters, hashes, sources=("paid",))
    assert report["stage"] == "partial"
    assert report["event_counts"] == {"paid": 0}
    assert not report["log_range_complete"] and not report["execution_eligible"]
    assert posters["public"].calls == []
    assert not (out / "segmented-report.json").exists()
    with RpcBudget(out / "budget.db", policies) as budget:
        assert budget.counts() == {"public": 6, "paid": 10}


def test_changed_archive_snapshot_is_refused_before_network(campaign):
    out, archive, posters, hashes, _ = campaign
    path = archive / "chunk-0000.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="frozen complete checkpoint"):
        cli.resume(out, archive, posters, hashes)
    assert all(not p.calls for p in posters.values())


@pytest.mark.parametrize("changed", ["endpoint", "manifest_cap", "lost_budget", "report"])
def test_binding_loss_or_tamper_refuses_without_network(campaign, changed):
    out, archive, posters, hashes, _ = campaign
    if changed == "endpoint":
        hashes = {**hashes, "public": "f" * 64}
    elif changed == "manifest_cap":
        m = json.loads((out / "manifest.json").read_bytes())
        m["max_requests_per_provider"] = 17
        (out / "manifest.json").write_bytes(canonical_json(m))
    elif changed == "lost_budget":
        (out / "budget.db").unlink()
    else:
        cli.resume(out, archive, posters, hashes)
        r = json.loads((out / "segmented-report.json").read_bytes())
        r["event_count"] = 1
        (out / "segmented-report.json").write_bytes(canonical_json(r))
    calls = {s: len(p.calls) for s, p in posters.items()}
    with pytest.raises(ValueError):
        cli.resume(out, archive, posters, hashes)
    assert calls == {s: len(p.calls) for s, p in posters.items()}
    if changed == "lost_budget":
        assert not (out / "budget.db").exists()


def test_http_400_rpc_error_body_is_retained(monkeypatch):
    def run(command, **kwargs):
        r = json.loads(kwargs["input"])
        return CompletedProcess(
            command,
            0,
            canonical_json(
                {
                    "jsonrpc": "2.0",
                    "id": r["id"],
                    "error": {"code": -32600, "message": "fixture provider refusal"},
                }
            )
            + b"\n400",
            b"sensitive stderr",
        )

    monkeypatch.setattr(cli.subprocess, "run", run)
    recorder = BoundedExchangeRecorder()
    poster = cli.RetainedErrorCurlPoster("https://fixture.invalid")
    rpc = JsonRpcTransport("https://fixture.invalid", http=poster, recorder=recorder, retries=0)
    with pytest.raises(RpcError):
        rpc.request("eth_getLogs", [{}])
    assert len(recorder.exchanges) == 1
    assert poster.last_http == {"http_status": 400, "curl_exit": 0}
    assert b"sensitive stderr" not in recorder.exchanges[0].response_payload
