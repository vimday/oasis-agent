"""Historical index leads spend the original budget and never create canonical coverage."""

import json

import httpx
import pytest

from oasis_agent.arbitrage.aave_history import SIGNATURES
from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.evidence.public_event_index import index_logs
from oasis_agent.evidence.public_rpc_plan import PublicEventIndexPlan
from oasis_agent.models.common import canonical_json
from scripts import capture_public_rpc as cli
from tests.unit.test_aave_history import ACCOUNT, POOL, event
from tests.unit.test_public_rpc_capture import counts


def plan():
    log = event("borrow")
    return PublicEventIndexPlan.model_validate_json(
        canonical_json(
            {
                "schema": "oasis-public-event-index-plan-v1",
                "source": "blockscout_ethereum",
                "chain_id": 1,
                "start_block": log.block_number - 1,
                "end_block": log.block_number + 1,
                "address": POOL,
                "account": ACCOUNT,
                "signature": topic(SIGNATURES["borrow"]),
                "account_topic_index": 2,
            }
        )
    )


def row():
    log = event("borrow", 3)
    return {
        "address": log.address,
        "topics": list(log.topics),
        "data": log.data,
        "blockNumber": hex(log.block_number),
        "blockHash": log.block_hash,
        "transactionHash": log.transaction_hash,
        "transactionIndex": "0x2",
        "logIndex": "0x3",
        "removed": False,
    }


@pytest.mark.parametrize("bad", ["account", "range", "removed", "duplicate", "cap", "null"])
def test_index_rejects_wrong_filter_or_ambiguous_coverage(bad):
    item = row()
    rows = [item]
    if bad == "account":
        item["topics"][2] = "0x" + "ff" * 32
    elif bad == "range":
        item["blockNumber"] = hex(plan().end_block + 1)
    elif bad == "removed":
        item["removed"] = True
    elif bad == "duplicate":
        rows *= 2
    elif bad == "cap":
        rows *= 1000
    else:
        rows = None
    with pytest.raises(ValueError):
        index_logs(plan(), rows)


@pytest.mark.parametrize("rate_limited", [False, True])
def test_normal_entry_pre_reserves_and_replays_index_observations(
    tmp_path, monkeypatch, rate_limited
):
    scope = tmp_path / "scope"
    root = tmp_path / "campaign"
    path = tmp_path / "plan"
    scope.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-rpc-scope-v1",
                "purpose": "index_test",
                "expires_utc": "2100-01-01T00:00:00Z",
                "max_capture_seconds": 20,
                "sources": [{"source": "blockscout_ethereum", "max_requests": 2}],
            }
        )
    )
    path.write_bytes(canonical_json(plan().model_dump(mode="json", by_alias=True)))
    cli.initialize(scope, root)
    actual = []
    original = httpx.Client

    def handler(req):
        packet = json.loads(req.content)
        actual.append(packet)
        assert counts(root) == {"blockscout_ethereum": len(actual)}
        assert req.url.host == "eth.blockscout.com"
        assert "authorization" not in req.headers
        if len(actual) == 2 and rate_limited:
            return httpx.Response(429, content=b"limited")
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": packet["id"],
                "result": "0x1" if len(actual) == 1 else [row()],
            },
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    assert cli.capture(root, path, "run") == (3 if rate_limited else 0)
    report = cli.replay(root / "run")
    assert report["plan_kind"] == "historical_index_leads"
    assert report["block"] is None and not report["execution_eligible"]
    assert report["reserved_requests"] == 2 and (root / "STOP").exists() == rate_limited
    assert actual[1]["params"][0]["topics"] == [
        topic(SIGNATURES["borrow"]),
        None,
        "0x" + ACCOUNT[2:].zfill(64),
    ]
