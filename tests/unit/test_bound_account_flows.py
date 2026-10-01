"""Production flow proofs bind every query to the two inventory snapshots' ancestry."""

import json
from dataclasses import replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.account_flows import (
    FlowScope,
    collect_token_flow_quorum,
    persist_token_flow_evidence,
    replay_token_flow_evidence,
)
from oasis_agent.models.common import canonical_json
from tests.unit.test_account_risk_reader import (
    ACCOUNT,
    BLOCK_HASH,
    BLOCK_NUMBER,
    BLOCK_TWO,
    CHAIN_ID,
    HASH_TWO,
    USDC_SPEC,
    InventoryFixture,
    bound_flow_fixture,
    flow_session,
    later,
    usdc_transfer,
)


def scope():
    return FlowScope(
        CHAIN_ID,
        ACCOUNT,
        "usdc",
        USDC_SPEC.address,
        6,
        BLOCK_NUMBER,
        BLOCK_TWO,
        start_block_hash=BLOCK_HASH,
        end_block_hash=HASH_TWO,
    )


def collect(*, incoming=(), outgoing=()):
    fixture = later(InventoryFixture())
    return collect_token_flow_quorum(
        flow_session("rpc_a", bound_flow_fixture(fixture, incoming=incoming, outgoing=outgoing)),
        flow_session("rpc_b", bound_flow_fixture(fixture, incoming=incoming, outgoing=outgoing)),
        scope=scope(),
    )


def test_hash_bound_empty_flow_replays_at_its_closing_snapshot(tmp_path):
    quorum = collect()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        refs = persist_token_flow_evidence(store, quorum)
        for ref in refs:
            assert ref.schema_version == 2 and ref.block.block_hash == HASH_TWO
            flow = replay_token_flow_evidence(
                store, ref, scope=scope(), tracked_accounts=frozenset({ACCOUNT})
            )
            assert flow.net_units == 0 and flow.logs == ()
            with pytest.raises(ValueError, match="unsupported"):
                replay_token_flow_evidence(
                    store,
                    replace(ref, schema_version=1),
                    scope=scope(),
                    tracked_accounts=frozenset({ACCOUNT}),
                )
        calls = [
            json.loads(e.request_payload)
            for e in quorum.left.exchanges
            if e.method == "eth_getLogs"
        ]
        assert all(c["params"][0]["blockHash"] == HASH_TWO for c in calls)
        assert all("fromBlock" not in c["params"][0] for c in calls)
    finally:
        store.close()


@pytest.mark.parametrize("index,field", [(1, "hash"), (2, "hash"), (2, "parentHash"), (5, "hash")])
def test_even_empty_flows_cannot_hide_reorgs_or_missing_ancestry(index, field):
    capture = collect().left
    exchanges = list(capture.exchanges)
    response = json.loads(exchanges[index].response_payload)
    response["result"][field] = "0x" + "ff" * 32
    exchanges[index] = replace(exchanges[index], response_payload=canonical_json(response))
    with pytest.raises(ValueError, match=r"boundaries|ancestry|changed"):
        replace(capture, exchanges=tuple(exchanges))


def test_a_log_from_another_fork_cannot_explain_the_balance():
    log = usdc_transfer(from_address="0x" + "e9" * 20, to_address=ACCOUNT, units=1)
    log["blockHash"] = "0x" + "ff" * 32
    with pytest.raises(ValueError, match="pinned block"):
        collect(incoming=(log,))


def test_self_transfer_returned_by_both_filters_is_one_zero_net_event():
    log = usdc_transfer(from_address=ACCOUNT, to_address=ACCOUNT, units=123)
    flow = collect(incoming=(log,), outgoing=(log,)).flow(tracked_accounts=frozenset({ACCOUNT}))
    assert len(flow.logs) == 1
    assert (
        flow.net_units,
        flow.external_net_units,
        flow.external_in_units,
        flow.external_out_units,
    ) == (0, 0, 0, 0)
    with pytest.raises(ValueError, match="duplicate"):
        collect(incoming=(log, log), outgoing=(log,))


def test_boundary_pair_and_capture_span_are_bounded():
    with pytest.raises(ValueError, match="both boundary"):
        replace(scope(), start_block_hash=None)
    with pytest.raises(ValueError, match="1000 blocks"):
        replace(scope(), end_block=BLOCK_NUMBER + 1001)
